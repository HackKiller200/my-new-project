# Восстановление доступа Argo CD к Kubernetes после обновления сертификатов

> **Production troubleshooting runbook** · Kubernetes (kubeadm) · Argo CD · air-gapped/offline · без `jq` и `argocd` CLI
>
> Версия документа: 2026-10-15. Используй команды по порядку. Команды, изменяющие кластер, помечены **ИЗМЕНЕНИЕ**.

---

## 0. Исходная ситуация и ожидаемый результат

**Сценарий:** кластер в VMware vCenter состоит из 1 control-plane/master и 3 workers. Сертификаты Kubernetes истекли, администратор обновил сертификаты и перезапустил `kubelet`. После этого локальные команды `kubectl` и поды работают, но Argo CD перестал синхронизировать приложения. В UI видна ошибка вроде:

```text
Cluster connection failed
Cluster status: Failed / Unknown
error synchronizing cache state:
the server has asked for the client to provide credentials
```

Дополнительно обнаруживались CSR в состоянии `Pending`; после их одобрения проблема Argo CD осталась. Обновление локального `kubeconfig`, сравнение токенов и перезапуск подов Argo CD проблему не решили.

**Цель:** понять, где разорвалась цепочка `Argo CD → Kubernetes API`, восстановить авторизацию **без переустановки**, без удаления Applications и с возможностью отката.

**Важное ограничение:** описанное — гипотеза, а не доказанный диагноз. Ошибка может быть вызвана просроченным **клиентским сертификатом Argo CD**, недействительным **bearer token**, сменой **CA/ключей подписи**, ограничением **RBAC**, невозможностью достучаться до API или иным механизмом подключения.

### Правило безопасности

Не выполняй наугад:

- `kubectl delete secret <cluster-secret>`;
- удаление кластера через веб-интерфейс Argo CD;
- повторную ротацию всех сертификатов;
- массовый `rollout restart`;
- удаление/одобрение всех CSR подряд;
- `insecureSkipTLSVerify: true` / `tlsClientConfig.insecure: true` для «починки»;
- запись токенов/приватных ключей в Git, чат или открытые логи.

Вначале диагностика, затем **одно** изменение, потом проверка.

---

## 1. Как работает подключение Argo CD к кластеру

```text
      ┌───────────────────────────────┐
      │ Кластер A: Argo CD           │
      │                               │
      │  argocd-application-controller│
      │              │                │
      │  Secret с данными кластера    │
      │  ├─ server                   │
      │  ├─ bearerToken              │
      │  ├─ tlsClientConfig.caData   │
      │  ├─ tlsClientConfig.certData │
      │  └─ tlsClientConfig.keyData  │
      └──────────────┬────────────────┘
                     │ HTTPS :6443
                     ▼
      ┌───────────────────────────────┐
      │ Кластер B: целевой Kubernetes │
      │ kube-apiserver                │
      │  1. TLS-проверка сервера      │
      │  2. Аутентификация клиента    │
      │  3. Авторизация (RBAC)        │
      │  4. CRUD/WATCH объектов      │
      └───────────────────────────────┘
```

Если Argo CD установлен в том же кластере, возможна **локальная** регистрация `https://kubernetes.default.svc`: она обычно использует ServiceAccount самого Argo CD, и отдельного cluster Secret может **не быть**. Этот сценарий разобран в разделе 12.

### Не путай четыре разных типа credentials

| Объект | Для чего | Где хранится |
|---|---|---|
| `kubeconfig` администратора (`admin.conf`) | Доступ человека через `kubectl` | На мастере / рабочей машине |
| Cluster Secret Argo CD | Доступ контроллера Argo CD к **внешнему** кластеру | Kubernetes Secret в namespace Argo CD |
| ServiceAccount token (`argocd-manager` или другой SA) | Идентичность Argo CD в целевом Kubernetes | На целевом кластере; значение токена может храниться в Argo CD |
| TLS-сертификаты kube-apiserver и клиентские сертификаты | Шифрование / удостоверение сторон | PKI control plane, kubeconfig, Argo CD Secret |

Обновление `/etc/kubernetes/admin.conf` **не обновляет автоматически** Cluster Secret Argo CD. Перезапуск подов не заменяет устаревшие credentials в этом Secret.

### Как трактовать ошибки

| Симптом | Что проверять первым |
|---|---|
| `the server has asked for the client to provide credentials` | Невалидный токен / клиентский сертификат; API ответил как неавторизованному |
| `Unauthorized`, HTTP `401` | Аутентификация не прошла |
| `Forbidden`, HTTP `403` | Аутентификация, как правило, прошла, но прав RBAC недостаточно; сверяй API-ответ и identity |
| `x509: certificate has expired` | Срок конкретного сертификата: серверного или клиентского |
| `certificate signed by unknown authority` | CA / цепочка доверия (часто `caData`) |
| `context deadline exceeded`, `i/o timeout` | Сеть, firewall, маршрут, доступность API |
| `no such host` | DNS для hostname API |
| `connection refused` | Адрес/порт/процесс API или балансировщик |
| `Unable to list ... forbidden` | RBAC, namespace restriction, AppProject |
| `Application OutOfSync`, но Cluster Successful | Git/Helm/SyncPolicy/прикладная ошибка, не обязательно credentials |

---

## 2. Где выполнять команды: не смешивай два кластера

Ниже используются **функции-обёртки** `argo` и `target`, чтобы нельзя было случайно исправить Secret не в том кластере.

**Вариант A — с одной администраторской машины доступны оба kubeconfig:**

```bash
# Укажи реальные пути к файлам. НЕ клади их в Git.
export ARGO_KUBECONFIG='/secure/path/argo-cluster.kubeconfig'
export TARGET_KUBECONFIG='/secure/path/target-cluster.kubeconfig'
export ARGO_NS='argocd'

argo()   { kubectl --kubeconfig "$ARGO_KUBECONFIG" "$@"; }
target() { kubectl --kubeconfig "$TARGET_KUBECONFIG" "$@"; }

argo config current-context
target config current-context
argo get nodes
target get nodes
```

**Вариант B — доступ только к одному кластеру с каждой машины:** команды с префиксом `argo` выполняй с машины/контекста, где установлен Argo CD; команды с `target` — на мастере проблемного кластера. Вместо функций используй обычный `kubectl` с нужным `KUBECONFIG`. Если Argo CD установлен на целевом кластере, пути к kubeconfig могут совпадать.

> Нельзя слепо применять команду `kubectl -n argocd ...` на целевом мастере, если Argo CD находится **на другом кластере**: нужного Secret там просто нет.

Проверка namespace и workload Argo CD:

```bash
argo get ns
argo -n "$ARGO_NS" get pods -o wide
argo -n "$ARGO_NS" get deployments,statefulsets
```

**Ожидаемо:** есть `argocd-application-controller` (часто StatefulSet), `argocd-server`, `argocd-repo-server` и другие установленные компоненты. Для Helm/operator/нестандартной установки названия могут отличаться.

---

## 3. Сначала: проверка здоровья целевого control plane (ТОЛЬКО ЧТЕНИЕ)

### 3.1. Состояние Kubernetes API и нод

```bash
target get nodes -o wide
target get --raw=/readyz
target get --raw=/livez
target -n kube-system get pods -o wide
```

Если `get --raw=/readyz` не поддерживается конкретным релизом, используй `target cluster-info` и `target get nodes`.

На **самом мастере** kubeadm-кластера:

```bash
sudo kubeadm certs check-expiration
sudo systemctl status kubelet --no-pager
sudo journalctl -u kubelet --since '2 hours ago' --no-pager | tail -n 100
date -u
```

Уточни реальную конфигурацию: если кластер развёрнут **не через kubeadm**, пути и процедуры PKI могут отличаться. Не применяй команды `kubeadm certs renew` к RKE, K3s, Talos, managed K8s и т. п.

### 3.2. Проверка даты и сертификата сервера

```bash
# На мастере kubeadm
sudo openssl x509 -in /etc/kubernetes/pki/apiserver.crt \
  -noout -subject -issuer -dates -ext subjectAltName

sudo openssl x509 -in /etc/kubernetes/pki/ca.crt \
  -noout -subject -issuer -dates
```

**Важно:** у `ca.crt` срок обычно намного больше года; обновление обычных leaf-сертификатов и **ротация CA** — совершенно разные операции. Если меняли `ca.crt`, старое `caData` в Argo CD может перестать доверять API. Но новый CA **не исправляет** недействительный bearer token.

Проверка клиентского сертификата администратора из актуального kubeconfig:

```bash
# kubeadm admin.conf включает client-certificate-data.
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf \
  config view --raw -o jsonpath='{.users[0].user.client-certificate-data}' \
  | base64 -d | openssl x509 -noout -subject -issuer -dates
```

**Осторожно:** этот шаблон выбирает первого пользователя из файла; при необычном kubeconfig со множеством записей сначала проверь `kubectl config get-contexts` и сопоставление context → user. Сам **сырой** kubeconfig не публикуй.

Если сертификаты ещё не применены после `kubeadm certs renew`, официальный Kubernetes требует перезапустить соответствующие static Pods control plane. **Обычный рестарт kubelet не гарантирует, что все компоненты подхватили новые сертификаты.** Работай с этим отдельно и только после подтверждения состояния API; способ перезапуска static Pod см. в официальном руководстве [kubeadm certificate management](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/).

### 3.3. CSR

```bash
target get csr -o wide
target get csr
# Для конкретного имени:
target describe csr '<CSR_NAME>'  # заменить на имя CSR
```

`Approved,Issued` означает, что **данный** CSR утверждён и подписан. Это **не значит**, что bearer token или сертификат в Argo CD Secret обновился. CSR часто связан с kubelet; проверяй `spec.signerName`, `spec.username`, запрошенные usages. Не выполняй массовое `approve` без проверки запроса.

---

## 4. Получи точную ошибку Argo CD (ТОЛЬКО ЧТЕНИЕ)

В веб-интерфейсе: **Settings → Clusters → проблемный кластер → Connection Status / Message**. Сохрани полный текст, а не только `Failed`.

На **кластере Argo CD**:

```bash
argo -n "$ARGO_NS" get pods | grep -Ei 'application-controller|server'

# Если это StatefulSet, при обычном имени:
argo -n "$ARGO_NS" logs statefulset/argocd-application-controller \
  --all-containers=true --since=2h --tail=1000 \
  | grep -Ei 'credential|unauthorized|forbidden|certificate|x509|auth|cluster|cache|timeout'

# Если workload называется по-другому, используй его реальное имя из get pods.
```

Логи по конкретному поду — альтернативный способ:

```bash
argo -n "$ARGO_NS" logs '<ARGO_CONTROLLER_POD_NAME>' \
  --all-containers=true --since=2h --tail=300
```

API server на **целевом** мастере:

```bash
target -n kube-system get pods -l component=kube-apiserver

target -n kube-system logs -l component=kube-apiserver \
  --all-containers=true --since=2h --tail=500 --prefix=true \
  | grep -Ei 'Unable to authenticate|x509|certificate|token|Unauthorized'
```

В некоторых конфигурациях логи аписервера доступны через `crictl`/журнал мастера; `kubectl logs -l` не обязан вернуть все исторические рестарты. Пустой `grep` не доказывает отсутствие ошибки.

---

## 5. Найди СВОЙ Cluster Secret (ТОЛЬКО ЧТЕНИЕ)

Обычная регистрация **внешнего** Kubernetes хранится в Secret с меткой `argocd.argoproj.io/secret-type=cluster`.

```bash
argo -n "$ARGO_NS" get secrets \
  -l argocd.argoproj.io/secret-type=cluster
```

Если кластеров много, распечатай **только имена и адреса API**, без `config`:

```bash
for item in $(argo -n "$ARGO_NS" get secrets \
  -l argocd.argoproj.io/secret-type=cluster -o name); do
  printf '%s -> ' "$item"
  argo -n "$ARGO_NS" get "$item" \
    -o jsonpath='{.data.server}' | base64 -d
  printf '\n'
done
```

Например:

```text
secret/cluster-production-4219 -> https://10.20.30.40:6443
```

Запиши **имя без `secret/`**:

```bash
export S='cluster-production-4219'  # ЗАМЕНИ на своё имя
```

Проверь metadata без секретных значений:

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.metadata.name}{"\n"}{.metadata.labels}{"\n"}{.metadata.annotations}{"\n"}'
```

**Если список пуст:** сначала перейди к разделу 12 (in-cluster) или проверь правильность кластера/namespace. Не создавай новый Secret поверх локальной регистрации вслепую.

**Если Secret управляется GitOps, External Secrets, Terraform, Helm или operator:** изменения вручную могут быть перезаписаны. Определи source of truth до `patch`.

---

## 6. Без jq: узнай, какая аутентификация настроена

### Вариант 1 — есть `python3` (надёжнее)

Команда ничего секретного не выводит; JSON внутри `config` разбирается корректно:

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d | python3 -c '
import json, sys
c = json.load(sys.stdin)
t = c.get("tlsClientConfig") or {}
for name, val in [
    ("bearerToken", c.get("bearerToken")),
    ("client certData", t.get("certData")),
    ("client keyData", t.get("keyData")),
    ("CA caData", t.get("caData")),
    ("execProviderConfig", c.get("execProviderConfig")),
    ("awsAuthConfig", c.get("awsAuthConfig")),
    ("basic username", c.get("username")),
]:
    print("{}: {}".format(name, "PRESENT" if val else "absent"))
print("tlsClientConfig.insecure:", bool(t.get("insecure", False)))
'
```

Пример интересного случая:

```text
bearerToken: PRESENT
client certData: PRESENT
client keyData: PRESENT
CA caData: PRESENT
execProviderConfig: absent
awsAuthConfig: absent
basic username: absent
tlsClientConfig.insecure: False
```

### Вариант 2 — нет `python3`, только POSIX-утилиты

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d \
  | grep -oE '"(bearerToken|certData|keyData|caData|execProviderConfig|awsAuthConfig|username)"[[:space:]]*:'
```

**Ограничение:** `grep` показывает лишь наличие названия ключа, а не непустое значение, не срок его действия и не фактическое использование. В случае сложного JSON предпочтительнее использовать Python на любой доверенной машине, а не менять конфигурацию через `sed`.

### Что означает каждая ветка

- **Есть `certData` + `keyData`:** обязательно проверить дату **клиентского** сертификата Argo CD (раздел 7).
- **Есть `bearerToken`:** обязательно проверить действительность токена и RBAC (разделы 8–9), даже если токен визуально совпадает с ожидаемым.
- **Есть обе пары:** возможен дефект из-за просроченного клиентского сертификата при наличии токена — известный реальный случай.
- **`execProviderConfig`/`awsAuthConfig`:** используется внешний механизм, например OIDC/IAM; не меняй его на ServiceAccount без отдельного плана.
- **Ничего из перечисленного:** перепроверь правильность Secret и используемый кластер, не пытайся сгенерировать случайный токен.

---

## 7. Проверить сертификат в Argo CD Secret (ТОЛЬКО ЧТЕНИЕ)

Здесь проверяем **`tlsClientConfig.certData` — клиентский сертификат**, который Argo CD может отправлять Kubernetes API. Не путать с `caData` — корнем доверия API.

### Если есть `python3`

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d | python3 -c '
import json, sys, base64, subprocess
c = json.load(sys.stdin)
b64 = (c.get("tlsClientConfig") or {}).get("certData")
if not b64:
    print("certData отсутствует")
    sys.exit(0)
cert = base64.b64decode(b64)
subprocess.run(
    ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates", "-serial"],
    input=cert, check=True)
'
```

### Если нет `python3`, но JSON содержит `certData` строкой

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d \
  | sed -n 's/.*"certData"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  | base64 -d \
  | openssl x509 -noout -subject -issuer -dates -serial
```

`sed` — **ограниченный запасной вариант** для JSON с обычным строковым полем, а не универсальный JSON-парсер. При ошибке `unable to load certificate` ничего не меняй, используй Python на доверенной машине.

Пример:

```text
subject=O = system:masters, CN = kubernetes-admin
notBefore=Oct 12 13:00:00 2025 GMT
notAfter=Oct 12 13:00:00 2026 GMT
```

На дату 15 октября 2026 года такой сертификат уже истёк. `CN=kubernetes-admin` указывает, что когда-то в Argo CD могли сохранить административный сертификат из kubeconfig — это нежелательно для долгосрочной эксплуатации.

**Проверка результата:** если клиентский сертификат истёк, возможны два правильных пути:

1. **Предпочтительно:** оставить **подтверждённо действительный** SA bearer token и убрать устаревшие `certData`/`keyData` (раздел 10A).
2. Если bearer token отсутствует/невалиден — восстановить **отдельный** ServiceAccount и его credential, затем обновить Cluster Secret (раздел 10B).

> Известный аналогичный инцидент Argo CD: https://github.com/argoproj/argo-cd/issues/19033 — одновременно были token и просроченный клиентский сертификат; API server отклонял сертификат. Но это не доказывает, что в данном кластере причина та же.

---

## 8. Проверить токен без его публикации

Задача — отделить:

- **токен есть в JSON**;
- **токен не истёк по JWT-claims**;
- **API фактически принимает токен**;
- **пользователь токена имеет нужный RBAC**.

Это **четыре разных утверждения**.

### 8.1. JWT-поля (опционально, только Python, без печати токена)

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d | python3 -c '
import json,sys,base64,datetime
c=json.load(sys.stdin)
s=c.get("bearerToken")
if not s:
    print("bearerToken отсутствует"); sys.exit(0)
p=s.split(".")
if len(p)!=3:
    print("Токен не похож на JWT; пропускаем проверку claims"); sys.exit(0)
try:
    payload=json.loads(base64.urlsafe_b64decode(p[1]+"="*(-len(p[1])%4)))
except Exception as exc:
    print("Невозможно прочитать JWT claims:", type(exc).__name__); sys.exit(0)
for name in ("iss","sub","aud"):
    print(name, ":", payload.get(name))
for name in ("iat","nbf","exp"):
    v=payload.get(name)
    print(name, ":", datetime.datetime.fromtimestamp(v,datetime.timezone.utc).isoformat() if isinstance(v,(int,float)) else v)
'
```

JWT без `exp` может быть legacy long-lived SA token; это **не доказательство**, что API его принимает. Декодирование claims **не проверяет подпись**. Валидность должен подтвердить Kubernetes API.

### 8.2. Лучший тест: TokenReview на целевом кластере (при доступе к обоим kubeconfig)

Если у тебя с одной доверенной машины есть доступ и к Argo CD, и к целевому кластеру, используй Kubernetes `TokenReview`. Это проверяет токен **на стороне API**, даже если RBAC не позволяет читать объекты.

> Для этого примера требуется `python3` на **администраторской машине**, где доступны оба kubeconfig. Сам токен не печатается, но лежит во временном JSON-файле. Файл должен быть защищён; не включай shell tracing (`set -x`).

```bash
umask 077
WORK=$(mktemp -d)
chmod 700 "$WORK"

argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d > "$WORK/argocd-config.json"

python3 - "$WORK/argocd-config.json" > "$WORK/tokenreview.json" <<'PY'
import json,sys
with open(sys.argv[1]) as f:
    cfg=json.load(f)
token=cfg.get("bearerToken")
if not token:
    raise SystemExit("bearerToken отсутствует")
print(json.dumps({
  "apiVersion":"authentication.k8s.io/v1",
  "kind":"TokenReview",
  "spec":{"token":token}
}))
PY

target create --raw /apis/authentication.k8s.io/v1/tokenreviews \
  -f "$WORK/tokenreview.json" \
  | python3 -c '
import json,sys
r=json.load(sys.stdin)
s=r.get("status") or {}
print("authenticated:",s.get("authenticated"))
print("username:",(s.get("user") or {}).get("username"))
print("error:",s.get("error"))
'
```

Ожидаемый результат:

```text
authenticated: True
username: system:serviceaccount:kube-system:argocd-manager
error: None
```

**Если `authenticated: False`:** Kubernetes не принимает токен; не удаляй из Argo CD клиентские credentials, пока не восстановишь рабочую аутентификацию.

**Если API отклонил сам запрос TokenReview как `Forbidden`:** прав администратора для проверки недостаточно; это **не означает**, что тестируемый токен недействителен.

После выполнения удали приватные временные файлы: сначала проверь `printf '%s\n' "$WORK"` и убедись, что это действительно созданный временный каталог. Затем используй `rm -rf -- "$WORK"` **только для проверенного каталога**, когда результаты теста уже зафиксированы и backup сохранён отдельно.

### 8.3. Альтернатива: проверить токен через изолированный kubeconfig

Этот тест проверяет и аутентификацию, и доступность API **с машины запуска**, но не обязательно из сетевого namespace Argo CD pod. Создаём kubeconfig, где **нет никакого клиентского сертификата**, чтобы существующий `admin.conf` не подменил идентичность.

```bash
# Требуется файл $WORK/argocd-config.json из п. 8.2
argo -n "$ARGO_NS" get secret "$S" -o json > "$WORK/cluster-secret.json"

python3 - "$WORK/cluster-secret.json" "$WORK/token-only.kubeconfig" <<'PY'
import json,sys,base64,os
with open(sys.argv[1]) as f:
    sec=json.load(f)
def dec(k):
    return base64.b64decode(sec["data"][k]).decode()
cfg=json.loads(dec("config"))
token=cfg.get("bearerToken")
if not token: raise SystemExit("В Secret нет bearerToken")
tls=cfg.get("tlsClientConfig") or {}
cl={"server":dec("server")}
if tls.get("caData"):
    cl["certificate-authority-data"]=tls["caData"]
if tls.get("serverName"):
    cl["tls-server-name"]=tls["serverName"]
if tls.get("insecure"):
    cl["insecure-skip-tls-verify"]=True  # только отражаем существующую настройку
obj={"apiVersion":"v1","kind":"Config",
     "clusters":[{"name":"target","cluster":cl}],
     "users":[{"name":"argo-token","user":{"token":token}}],
     "contexts":[{"name":"test","context":{"cluster":"target","user":"argo-token"}}],
     "current-context":"test"}
with open(sys.argv[2],"w") as f: json.dump(obj,f)
os.chmod(sys.argv[2],0o600)
PY

kubectl --kubeconfig "$WORK/token-only.kubeconfig" auth whoami
kubectl --kubeconfig "$WORK/token-only.kubeconfig" auth can-i list pods --all-namespaces
```

`auth whoami` доступен не во всех старых версиях `kubectl`; при отсутствии используй `kubectl ... get pods -A`, анализируя **именно** `401` против `403`. TokenReview более надёжен для чистой проверки идентичности.

**ИТОГ:** `TokenReview=True` и правильный `system:serviceaccount:...` + отсутствие expired `certData` — сильное подтверждение, что можно переключить Argo CD на токен.

---

## 9. RBAC: что можно делать ServiceAccount

На **целевом** кластере выясни, как именно называется SA. Для типичного `argocd cluster add`:

```bash
target -n kube-system get sa argocd-manager
target get clusterrole argocd-manager-role
target get clusterrolebinding argocd-manager-role-binding
```

Если этих имён нет — это **не ошибка сама по себе**: могла быть ручная регистрация с другой SA или namespace. Установи настоящую identity по TokenReview.

Проверка **RBAC без использования самого токена** — это проверка прав через impersonation:

```bash
target auth can-i list pods --all-namespaces \
  --as=system:serviceaccount:kube-system:argocd-manager

target auth can-i create deployments -n '<APP_NAMESPACE>' \
  --as=system:serviceaccount:kube-system:argocd-manager

target auth can-i patch deployments -n '<APP_NAMESPACE>' \
  --as=system:serviceaccount:kube-system:argocd-manager
```

> `--as` **НЕ** подтверждает, что настоящий токен рабочий. Это только симуляция RBAC от заданной identity; для команды у администратора должны быть права impersonate. Результаты `yes` не отменяют `401 Unauthorized` у Argo CD.

Если аутентификация успешно проходит, а запрос Argo CD получает `403`, проверяй RoleBinding/ClusterRoleBinding, ограничения `namespaces`, `clusterResources`, AppProject и policies. Не выдавай `cluster-admin` только из-за первого `Forbidden` — определяй нужные права по фактической операции.

---

## 10. Исправление: выбор по результатам диагностики

### Матрица решений

| Что установлено | Правильная ветка |
|---|---|
| Истёк `certData`, `bearerToken` **прошёл TokenReview** | **10A:** убрать `certData`/`keyData` из Argo CD Secret, оставить токен и CA |
| Bearer token отклонён, ServiceAccount существует | **10B:** создать/восстановить корректный SA credential и обновить `bearerToken` |
| TLS trust (`unknown authority`) | **10C:** проверить/обновить `caData` правильным доверенным CA |
| `401` нет, зато `403` | **раздел 9:** исправить RBAC |
| `timeout`, `no such host` | **раздел 11:** сеть/адрес/DNS |
| Argo CD работает **внутри** целевого кластера без Cluster Secret | **раздел 12:** in-cluster ServiceAccount и RBAC |

### 10A. ИЗМЕНЕНИЕ: убрать просроченный клиентский сертификат, сохранив токен

**Только если выполнены ВСЕ условия:**

1. Ты работаешь с **правильным** Cluster Secret внешнего кластера.
2. Сертификат `certData` действительно просрочен / неверен.
3. Есть `bearerToken`; **TokenReview подтвердил его работоспособность**, identity соответствует ожидаемой SA.
4. В Secret нет особого механизма, требующего mTLS (когда клиентский сертификат обязателен); это типичная Kubernetes API bearer-аутентификация.
5. Конфигурацию не перезапишет внешний оператор/манифест (либо source of truth уже согласован).

**Шаг 1. Секретный backup — до любого patch.**

```bash
umask 077
BACKUP_DIR=$(mktemp -d)
chmod 700 "$BACKUP_DIR"

argo -n "$ARGO_NS" get secret "$S" -o json > "$BACKUP_DIR/cluster-secret.before.json"
chmod 600 "$BACKUP_DIR/cluster-secret.before.json"
```

**Шаг 2. Подготовить patch локально.** Не выводим токен в терминал:

```bash
python3 - "$BACKUP_DIR/cluster-secret.before.json" > "$BACKUP_DIR/remove-client-cert.patch.json" <<'PY'
import json,sys,base64
with open(sys.argv[1]) as f:
    sec=json.load(f)
cfg=json.loads(base64.b64decode(sec["data"]["config"]))
if not cfg.get("bearerToken"):
    raise SystemExit("STOP: нет bearerToken")
tls=cfg.get("tlsClientConfig") or {}
if not (tls.get("certData") and tls.get("keyData")):
    raise SystemExit("STOP: нет полной пары certData/keyData")
tls.pop("certData",None)
tls.pop("keyData",None)
# caData, serverName, insecure и прочие параметры остаются без изменений
print(json.dumps({"data":{"config":base64.b64encode(json.dumps(cfg).encode()).decode()}}))
PY
chmod 600 "$BACKUP_DIR/remove-client-cert.patch.json"
```

**Шаг 3. Применить только поле `data.config`.** Не меняем `name`, `server`, label, AppProject, namespaces, остальные secret data:

```bash
argo -n "$ARGO_NS" patch secret "$S" \
  --type=merge --patch-file "$BACKUP_DIR/remove-client-cert.patch.json"
```

Если версия `kubectl` не поддерживает `--patch-file`, применяй patch из файла способом, который поддерживается твоей версией. Не вставляй токен руками в shell-команду: аргументы процесса могут быть видны другим пользователям. В современных версиях `kubectl` `--patch-file` предпочтительнее.

**Шаг 4. Убедиться, что Secret действительно изменён:**

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d | python3 -c '
import json,sys
c=json.load(sys.stdin); t=c.get("tlsClientConfig") or {}
print("bearerToken:",bool(c.get("bearerToken")))
print("certData:",bool(t.get("certData")))
print("keyData:",bool(t.get("keyData")))
print("caData:",bool(t.get("caData")))
'
```

Нормально: `bearerToken=True`, `certData=False`, `keyData=False`, а `caData` такой же, как прежде.

**Шаг 5. Проверка работы Argo CD (раздел 13).** Большинство стандартных конфигураций обрабатывают изменение Secret через watch; **не делай автоматический рестарт всего Argo CD**. Если после разумного времени контроллер продолжает показывать старое состояние и новая версия Secret не перечитывается, проверь журнал и только затем рассматривай управляемый restart **конкретного** `argocd-application-controller`.

### 10B. ИЗМЕНЕНИЕ: восстановить недействительный ServiceAccount bearer token

**Условия:** TokenReview показал `authenticated: false`, подтверждено, что внешний кластер должен использовать ServiceAccount `argocd-manager` (или другую известную SA), а права RBAC настроены корректно.

**Не делай** `kubectl create token ...` и не записывай результат на годы в Argo CD: токен из TokenRequest обычно **короткоживущий**, истекает по времени и снова оборвёт доступ.

Для совместимого offline-сценария допустим **долгоживущий токен** через Secret типа `kubernetes.io/service-account-token`. Такой credential несёт больший риск, требует контроля доступа, политики ротации и безопасного хранения. Он совместим с распространённой моделью внешнего Argo CD; в более современных архитектурах предпочтителен механизм автоматической ротации/федерации, если он поддержан окружением.

На целевом кластере сначала **подтверди** SA:

```bash
target -n kube-system get serviceaccount argocd-manager -o yaml
# Проверь существующие Secret-объекты, не удаляя их:
target -n kube-system get secrets
```

Если SA действительно существует и согласовано создание отдельного токена:

```bash
# ВНИМАНИЕ: ИЗМЕНЕНИЕ на целевом кластере.
cat <<'YAML' | target apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: argocd-manager-recovery-token
  namespace: kube-system
  annotations:
    kubernetes.io/service-account.name: argocd-manager
type: kubernetes.io/service-account-token
YAML
```

Проверить, что token controller заполнил поле `.data.token` (значение не показывать):

```bash
target -n kube-system get secret argocd-manager-recovery-token \
  -o go-template='{{if .data.token}}TOKEN_READY{{else}}TOKEN_NOT_READY{{end}}{{"\n"}}'
```

Если `TOKEN_NOT_READY`, проверить аннотацию, существование SA, service-account-token controller, подпись токенов, логи controller-manager. Не заполняй Secret произвольным токеном вручную.

**Если обе kubeconfig доступны с одной защищённой машины**, безопасно перенеси токен в подготовленный patch без вывода на экран:

```bash
# Предполагается, что $BACKUP_DIR содержит резервную копию из 10A,
# либо создаём её прямо сейчас:
umask 077
BACKUP_DIR=${BACKUP_DIR:-$(mktemp -d)}
chmod 700 "$BACKUP_DIR"
argo -n "$ARGO_NS" get secret "$S" -o json > "$BACKUP_DIR/cluster-secret.before-token.json"
target -n kube-system get secret argocd-manager-recovery-token \
  -o json > "$BACKUP_DIR/sa-token-source.json"
chmod 600 "$BACKUP_DIR"/*.json

python3 - "$BACKUP_DIR/cluster-secret.before-token.json" \
  "$BACKUP_DIR/sa-token-source.json" \
  > "$BACKUP_DIR/new-bearer-token.patch.json" <<'PY'
import sys,json,base64
with open(sys.argv[1]) as f: sec=json.load(f)
with open(sys.argv[2]) as f: sa=json.load(f)
cfg=json.loads(base64.b64decode(sec["data"]["config"]))
token=base64.b64decode(sa["data"]["token"]).decode()
if not token: raise SystemExit("STOP: пустой SA token")
cfg["bearerToken"]=token
tls=cfg.get("tlsClientConfig") or {}
# Удалять старый клиентский сертификат можно только если
# осознанно переходим на bearer auth и mTLS не требуется.
tls.pop("certData",None)
tls.pop("keyData",None)
print(json.dumps({"data":{"config":base64.b64encode(json.dumps(cfg).encode()).decode()}}))
PY
chmod 600 "$BACKUP_DIR/new-bearer-token.patch.json"
```

**Перед patch обязательно протестируй новый токен**, а не старый. В следующем примере новый credential читается из сохранённого `sa-token-source.json`, а проверяется через Kubernetes API целевого кластера. Никаких токенов в терминале:

```bash
python3 - "$BACKUP_DIR/sa-token-source.json" \
  > "$BACKUP_DIR/new-token-review.json" <<'PY'
import json,sys,base64
with open(sys.argv[1]) as f:
    sec=json.load(f)
token=base64.b64decode(sec["data"]["token"]).decode()
print(json.dumps({"apiVersion":"authentication.k8s.io/v1",
                  "kind":"TokenReview", "spec":{"token":token}}))
PY
chmod 600 "$BACKUP_DIR/new-token-review.json"

target create --raw /apis/authentication.k8s.io/v1/tokenreviews \
  -f "$BACKUP_DIR/new-token-review.json" \
  | python3 -c '
import json,sys
s=(json.load(sys.stdin).get("status") or {})
print("authenticated:", s.get("authenticated"))
print("username:", (s.get("user") or {}).get("username"))
print("error:", s.get("error"))
'
```

Продолжай **только если** `authenticated: True`, а `username` соответствует ожидаемой SA. Дальше проверь её RBAC (раздел 9) и что `server` Cluster Secret указывает именно на этот целевой кластер. Если существовала другая SA/namespace — поменяй примеры под неё.

```bash
# ИЗМЕНЕНИЕ. Только после проверки нового токена.
argo -n "$ARGO_NS" patch secret "$S" \
  --type=merge --patch-file "$BACKUP_DIR/new-bearer-token.patch.json"
```

**После успеха:** задокументируй владельца и политику ротации, держи новый токен в защищённом хранилище. Старый credential отзывай **только после** подтверждения, что Argo CD реально перешёл на новый. Не удаляй без разбора старые `kube-system` Secrets — они могут обслуживать другие интеграции.

**Если `argocd-manager` SA вообще не существует:** требуется спланировать новую SA, RBAC и её регистрацию. Не создавай `cluster-admin` вслепую; сначала восстанови из Git/Helm/backup первоначальную модель доступа. Если в среде намеренно используется `argocd-manager` с cluster-admin, решение о выдаче таких прав принимается отдельно.

### 10C. ИЗМЕНЕНИЕ: сменился CA Kubernetes API

Признаки: у Argo CD ошибка типа `x509: certificate signed by unknown authority` или верификация сертификата сервера не проходит. **Не путай с истёкшим клиентским `certData`.**

На целевом kubeadm-мастере:

```bash
sudo openssl x509 -in /etc/kubernetes/pki/ca.crt \
  -noout -subject -issuer -fingerprint -sha256
```

Сравни с тем CA, который Argo CD использует как `tlsClientConfig.caData`:

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.config}' | base64 -d | python3 -c '
import json,sys,base64,subprocess
v=(json.load(sys.stdin).get("tlsClientConfig") or {}).get("caData")
if not v: print("caData отсутствует"); sys.exit(0)
subprocess.run(["openssl","x509","-noout","-subject","-fingerprint","-sha256"],
    input=base64.b64decode(v),check=True)
'
```

Если CA действительно изменился и API обслуживается сертификатом от нового CA, нужен **проверенный** trust bundle, который подписывает именно server TLS сертификат. На доверенной администраторской машине с доступом к обоим кластерам получи **только публичный CA-сертификат** целевого API (не копируй `ca.key`). Если это kubeadm и API использует `/etc/kubernetes/pki/ca.crt`, перенеси сертификат **по защищённому каналу** в файл `$BACKUP_DIR/target-api-ca.crt`.

> В случае внешнего балансировщика, прокси или кастомной PKI истинный CA может отличаться от `ca.crt` на мастере. Предварительно проверь цепочку сертификатов реально используемого API URL и SAN. Не подменяй `caData` неподтверждённым сертификатом.

После сохранения старого Cluster Secret (как в 10A) можно сделать точечный patch:

```bash
# ИЗМЕНЕНИЕ. Нужен СУЩЕСТВУЮЩИЙ backup и доверенный PEM CA.
# Если $BACKUP_DIR ещё не задан, сначала выполни backup из 10A.
# $BACKUP_DIR/target-api-ca.crt — ПУБЛИЧНЫЙ PEM сертификат CA.

python3 - "$BACKUP_DIR/cluster-secret.before.json" \
  "$BACKUP_DIR/target-api-ca.crt" \
  > "$BACKUP_DIR/new-ca.patch.json" <<'PY'
import json,sys,base64
with open(sys.argv[1]) as f: sec=json.load(f)
with open(sys.argv[2],'rb') as f: ca=f.read()
if b'BEGIN CERTIFICATE' not in ca:
    raise SystemExit('STOP: CA PEM не найден')
cfg=json.loads(base64.b64decode(sec['data']['config']))
cfg.setdefault('tlsClientConfig',{})['caData']=base64.b64encode(ca).decode()
print(json.dumps({'data':{'config':base64.b64encode(json.dumps(cfg).encode()).decode()}}))
PY
chmod 600 "$BACKUP_DIR/new-ca.patch.json"

# Применяй только после подтверждения правильной CA:
argo -n "$ARGO_NS" patch secret "$S" \
  --type=merge --patch-file "$BACKUP_DIR/new-ca.patch.json"
```

Patch меняет только `tlsClientConfig.caData`, сохраняя другие поля. После него проверь Status кластера (раздел 13). Если API выдаёт сертификат на иной DNS/IP, добавление CA само по себе **не исправит** несовпадение SAN или просроченный сертификат сервера.

Если менялась **сама CA** Kubernetes, проверь не только Argo CD: kubelets, kubeconfigs, webhooks, cert trust и сервисные интеграции могут нуждаться в обновлении. Ротация CA — отдельный change с планом и откатом.

---

## 11. Если проблема не в токене: сеть, TLS, DNS, время

Проверки проводятся **из сети/узла, где реально работает Argo CD**, насколько это возможно. Успешный `kubectl get nodes` на мастере **не доказывает** сетевую доступность API из пода Argo CD.

Адрес API берётся из Cluster Secret:

```bash
argo -n "$ARGO_NS" get secret "$S" \
  -o jsonpath='{.data.server}' | base64 -d; echo
```

### 11.1. TCP и DNS

С хоста/пода, откуда Argo CD подключается к API (если нужные утилиты уже установлены):

```bash
# ЗАМЕНИ HOST и PORT на адрес из Secret
getent hosts '<API_HOST>'
nc -vz '<API_HOST>' 6443
curl -v --connect-timeout 5 'https://<API_HOST>:6443/version'
```

Отсутствие утилиты внутри контейнера не является проблемой Kubernetes. Не скачивай что-либо в offline-контур только ради этих команд. Для сетевой диагностики можно использовать уже развёрнутый диагностический pod или проверку с хоста, где установлен Argo CD, с оговоркой о различии сетевых путей.

### 11.2. Сертификат, предъявляемый сервером

```bash
openssl s_client -connect '<API_HOST>:6443' \
  -servername '<API_DNS_NAME>' -showcerts </dev/null \
  2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

Если API адресуется по IP, проверяй, что IP входит в SAN сертификата API. При TLS-проверках различай SNI/hostname и цепочку CA. Сертификат, который виден на API endpoint, может обслуживаться балансировщиком или TLS-прокси, а не локальным `apiserver.crt`.

### 11.3. Часы

```bash
# На control-plane и на нодах Argo CD
 date -u
```

Сильная рассинхронизация времени влияет на `notBefore/notAfter`, JWT `iat/nbf/exp` и другие проверки. Также смотри NetworkPolicy, firewall, маршруты, прокси `proxyUrl` в Cluster Secret, IP/LB и доступность `:6443`.

---

## 12. Специальный случай: Argo CD установлен В ЭТОМ ЖЕ кластере

Проверь `Application.spec.destination.server`. Для in-cluster обычно используется:

```text
https://kubernetes.default.svc
```

В стандартной локальной регистрации отдельный Cluster Secret с `bearerToken` может не понадобиться. Контроллер Argo CD использует **свой ServiceAccount** и его автоматически монтируемый/проецируемый токен.

Проверки:

```bash
argo -n "$ARGO_NS" get deployments,statefulsets
argo -n "$ARGO_NS" get serviceaccounts
argo -n "$ARGO_NS" get pods -o wide
```

Найди SA конкретного pod:

```bash
argo -n "$ARGO_NS" get pod '<ARGO_CONTROLLER_POD_NAME>' \
  -o jsonpath='{.spec.serviceAccountName}{"\n"}'
```

Проверь RBAC этой SA:

```bash
target auth can-i list pods --all-namespaces \
  --as='system:serviceaccount:argocd:<ARGO_CONTROLLER_SA>'
```

Посмотри монтирование SA token и `automountServiceAccountToken`, но **не публикуй** сам токен. Если стандартный SA token проецируется через TokenRequest API, Kubernetes ротирует его автоматически. Нельзя просто подменить его произвольным «вечным токеном» без понимания controller auth.

Если `https://kubernetes.default.svc` не резолвится из Argo CD, проверяй CoreDNS/Service `kubernetes`, CNI и NetworkPolicy. Если токен не подходит, исследуй serviceaccount audience, issuer, projected volume и API authentication; не удаляй Secret внешнего кластера, которого нет.

Также проверь, не зарегистрирован ли тот же физический кластер второй раз через **внешний IP**: у него может быть отдельный Cluster Secret, а приложения могут смотреть именно на него. Сопоставляй точный `Application.spec.destination.server` и `.data.server` Secret.

---

## 13. Верификация после исправления

### 13.1. API и RBAC на целевом Kubernetes

```bash
target get nodes -o wide
target get --raw=/readyz
# Для нового/исправленного ServiceAccount — RBAC через --as, см. раздел 9.
```

### 13.2. Состояние Argo CD

В UI **Settings → Clusters**: `Connection Status: Successful` или аналогичный статус. Проверь **точный целевой кластер**, не любой зелёный.

```bash
argo -n "$ARGO_NS" get pods
argo -n "$ARGO_NS" logs '<ARGO_CONTROLLER_POD_NAME>' \
  --since=10m --tail=300 \
  | grep -Ei 'credential|unauthorized|forbidden|x509|error synchronizing cache'
```

### 13.3. Проверка конкретного Application

```bash
argo -n "$ARGO_NS" get applications.argoproj.io
argo -n "$ARGO_NS" get application '<APP_NAME>' -o yaml
```

Смотри `status.conditions`, `status.sync.status`, `status.health.status`, `status.operationState` и `spec.destination`. Если используется другая namespace для Application CR, укажи её.

1. Убедись, что `ComparisonError`/`Failed to load live state` исчезли.
2. Запусти **обычный Refresh** проблемного Application через UI.
3. Если кластер стал доступен и манифесты корректно сравниваются — выполни **контролируемый Sync** для одного безопасного приложения.
4. Проверь, что ресурсы в целевом namespace реально обновились; `Synced` и `Healthy` — разные статусы.
5. Не запускай массовый sync сразу после изменения credentials.

### 13.4. Если Secret исправлен, но Argo CD упорно показывает старое

Сначала проверь watch/логи контроллера и корректность Server URL. В стандартной установке обновление cluster Secret подхватывается автоматически. Только при доказанной необходимости и с учётом HA/окна работ можно **управляемо** перезапустить именно контроллер:

```bash
argo -n "$ARGO_NS" get statefulsets,deployments
# Пример, только если StatefulSet действительно существует:
argo -n "$ARGO_NS" rollout restart statefulset/argocd-application-controller
argo -n "$ARGO_NS" rollout status statefulset/argocd-application-controller
```

Перезапуск НЕ исправляет неверный credential в Secret и может прервать reconcile. Не перезапускай все поды `argocd` наугад.

---

## 14. Откат (Rollback) после неудачного `patch`

Критически важно: **backup Cluster Secret должен быть сохранён ДО первой правки**. Из полного backup восстановим **только** исходное `.data.config`, не трогая metadata, `resourceVersion` и другие поля.

Для исправления по разделу 10A:

```bash
python3 - "$BACKUP_DIR/cluster-secret.before.json" \
  > "$BACKUP_DIR/rollback.patch.json" <<'PY'
import json,sys
with open(sys.argv[1]) as f:
    sec=json.load(f)
old=sec["data"]["config"]
print(json.dumps({"data":{"config":old}}))
PY
chmod 600 "$BACKUP_DIR/rollback.patch.json"

# ИЗМЕНЕНИЕ: возвращаем старый config (и исходную проблему, если она была в нём).
argo -n "$ARGO_NS" patch secret "$S" \
  --type=merge --patch-file "$BACKUP_DIR/rollback.patch.json"
```

Если использовалась ветка 10B, входной backup может называться `cluster-secret.before-token.json` — укажи **именно фактический** резервный файл.

Откат возвращает прежние credentials, поэтому не обещает успешного подключения; он только отменяет текущую правку. **Не удаляй** рабочий новый ServiceAccount token Secret до окончания проверки.

После завершения инцидента перенеси необходимые backups в защищённое хранилище с контролем доступа и сроком хранения, а лишние временные файлы безопасно удали. Не коммить файлы `*.before.json`, `*token*`, `*patch.json`, `*.kubeconfig` в репозиторий.

---

## 15. Решение проблем: короткие алгоритмы

### А. `kubectl` на мастере работает, Argo CD всё равно `401`

1. Определи, **из какого** кластера Argo CD подключается и каков его реальный `server`.
2. Найди правильный Cluster Secret.
3. Проверь `certData` (`openssl x509 -dates`) и `bearerToken` (`TokenReview`).
4. Если истёк клиентский сертификат, а токен валиден — 10A.
5. Если токен не проходит — 10B (после проверки SA/RBAC/issuer).
6. Если секретов нет, изучи in-cluster сценарий 12.

### B. `401` сменился на `403`

Аутентификация, вероятно, восстановлена. Теперь проверь RBAC для настоящей SA и разрешённые namespaces/cluster resources. Не ротируй сертификаты повторно.

### C. `x509: unknown authority`

Сверь CA, `caData`, реальный API endpoint и серверный сертификат. Если CA менялась, планируется доверенная цепочка (10C).

### D. `context deadline exceeded`

Проверь DNS, TCP:6443, NetworkPolicy, LB, firewall, маршруты из Argo CD к целевому API. Токен в этом случае может быть исправен.

### E. Cluster `Successful`, Application не деплоится

Проверь `Application` destination, AppProject restrictions, AppSet, repo-server доступ к Git/Helm и обычные Sync Errors. Cluster connectivity — лишь часть цепочки доставки.

### F. CSR `Approved,Issued`, а Argo CD нет

CSR не обязан относиться к credential Argo CD. Не связывай эти события причинно без `signerName`, requestor, времени и журналов API.

### G. `argocd-application-controller` живой, но кластер `Failed`

Состояние `Running`/`Ready` означает здоровье самого pod, а не валидность учётных данных в **каждом целевом** кластере.

### H. Токен «одинаковый», но аутентификация не работает

Совпадение строк не равно валидации. Проверь `TokenReview`: подпись, issuer, audience, срок, принадлежность SA, существование SA/Secret, конфигурацию API server; затем RBAC. Если одновременно задан client cert, API может обрабатывать и его.

---

## 16. Что собрать для удалённого разбора, НЕ раскрывая секреты

Можно отправить:

```text
1. Argo CD в этом же кластере? Да/Нет
2. kubeadm? Версия Kubernetes?
3. URL API, где находится target (можно замаскировать IP/hostname)
4. Точный текст Connection Error (без токена)
5. Вывод kubeadm certs check-expiration (без приватных ключей)
6. Вывод проверки PRESENT/absent по разделу 6
7. openssl -dates -subject -issuer клиентского certData (раздел 7)
8. TokenReview: authenticated, username, error (раздел 8)
9. RBAC auth can-i для SA
10. Логи контроллера с удалёнными идентификаторами/секретами
```

Не отправляй `bearerToken`, `.data.config`, `keyData`, содержимое `admin.conf`, full Secret JSON, TokenReview request body, Kubernetes JWT или закрытый ключ CA.

---

## 17. Последовательность действий в реальном инциденте (чеклист)

- [ ] Установлен правильный `kubectl` context для Argo и target.
- [ ] Целевой `kube-apiserver` доступен, `/readyz` успешен.
- [ ] Сверено время, проверены сертификаты kubeadm.
- [ ] Считан точный текст ошибки Argo CD и логи controller.
- [ ] Найден **правильный** Cluster Secret по `.data.server`.
- [ ] Без раскрытия данных определены `bearerToken`, `certData`, `keyData`, `caData`.
- [ ] Проверен срок клиентского `certData`.
- [ ] Проверен bearer token через `TokenReview` (если используется).
- [ ] Проверен RBAC фактической identity.
- [ ] Сделан защищённый backup Cluster Secret.
- [ ] Выбрана ровно одна ветка: 10A / 10B / 10C / сеть / in-cluster.
- [ ] Изменение внесено точечно, без удаления Applications.
- [ ] Cluster → `Successful`, Application refresh, один контролируемый sync.
- [ ] Ошибки из логов исчезли, создана запись об инциденте.
- [ ] Назначена профилактическая проверка срока сертификатов и ротации токенов.

---

## 18. Профилактика

1. **Проверяй сертификаты заранее:** `kubeadm certs check-expiration` и оповещение задолго до `notAfter`.
2. **Следи за SA credentials Argo CD:** знай, какой именно Secret/TokenRequest/OIDC используется; задокументируй issuer, audience, механизм ротации.
3. **Не добавляй административный TLS-клиентский сертификат в cluster Secret без необходимости.** Для внешнего кластера используй специально ограниченную identity.
4. **Ограничивай RBAC:** минимально необходимые действия/namespace вместо бездумного cluster-admin; учитывай, что Argo CD обычно нуждается в широком read/watch для сравнения состояния.
5. **Используй GitOps для конфигурации подключения**, но не храни plaintext токены/ключи в Git. Подойдут средства безопасного управления секретами, доступные в закрытом контуре.
6. **Следи за status кластера и логами `argocd-application-controller`**; ранняя тревога `ComparisonError` позволит избежать сюрпризов при релизе.
7. **Проверяй все интеграции после PKI change**: kubelets, webhooks, Argo CD, мониторинг, внешние CI/CD/операторы.
8. **Не путай `Running`, `Healthy`, `Synced` и `Connected`:** это проверки разных уровней.

---

## 19. Ссылки на официальную документацию и подтверждённый кейс

- [Argo CD — Declarative setup: Cluster Secrets](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/) — поля `server`, `config`, `bearerToken`, `tlsClientConfig`.
- [Argo CD — Security: External Cluster Credentials](https://argo-cd.readthedocs.io/en/stable/operator-manual/security/) — хранение и управление внешними credentials, `argocd-manager`.
- [Argo CD — Getting started: Register a Cluster](https://argo-cd.readthedocs.io/en/stable/getting_started/) — роль регистрации кластера и ServiceAccount.
- [Argo CD GitHub issue #19033](https://github.com/argoproj/argo-cd/issues/19033) — просроченный клиентский сертификат в cluster Secret при наличии bearer token.
- [Kubernetes — kubeadm Certificate Management](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/) — обновление и рестарт control plane static Pods.
- [Kubernetes — Configure Service Accounts for Pods](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/) — ServiceAccount и токены.
- [Kubernetes — Secrets](https://kubernetes.io/docs/concepts/configuration/secret/) — `kubernetes.io/service-account-token`.
- [Kubernetes — TokenReview API](https://kubernetes.io/docs/reference/kubernetes-api/authentication-resources/token-review-v1/) — проверка authentication.

---

## 20. Самый короткий безопасный путь для данного инцидента

Если нужно начать диагностику **прямо сейчас**, выполни **только чтение**, в этом порядке:

```bash
# На целевом мастере:
sudo kubeadm certs check-expiration
kubectl get nodes
kubectl get csr

# На кластере, где установлен Argo CD:
kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=cluster

# Найди нужный Secret по server (раздел 5) и укажи его имя:
S='<ИМЯ_CLUSTER_SECRET>'

# Определи доступные механизмы (названия полей, не значения):
kubectl -n argocd get secret "$S" -o jsonpath='{.data.config}' \
 | base64 -d \
 | grep -oE '"(bearerToken|certData|keyData|caData)"[[:space:]]*:'

# Если есть certData — проверь notAfter (раздел 7).
# Если есть bearerToken — проверь TokenReview (раздел 8).
# До установления причины ничего не удаляй и не патчи.
```

**Ключевой принцип:** сначала докажи, что именно Kubernetes API отвергает — клиентский сертификат или токен, — и только потом меняй Cluster Secret.

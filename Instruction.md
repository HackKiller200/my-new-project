```markdown
# OpenLens Node Shell

## Цель

Настроить OpenLens так, чтобы при выборе Kubernetes worker-ноды и нажатии:

Nodes → <worker-node> → Shell

внутри OpenLens открывался терминал именно этой worker-ноды.

SSH для этого не используется.

OpenLens под капотом создаёт временный `node-shell-*` Pod в `kube-system`, запускает его на выбранной Node и через `nsenter` открывает host shell.

---

## 1. Установить Node/Pod Menu extension

Начиная с OpenLens 6.3.0 часть меню Node/Pod была убрана из стандартной сборки.

Открыть:

OpenLens → Extensions

Установить:

`@alebcay/openlens-node-pod-menu`

После установки проверить:

Enabled

Полностью перезапустить OpenLens.

После перезапуска открыть:

Cluster → Nodes → выбрать worker-node

В меню ноды должен появиться пункт:

Shell

---

## 2. Настроить Node Shell Image

Открыть настройки нужного Kubernetes-кластера:

Cluster Settings → Node Shell

Найти:

Node Shell Image

Если worker-ноды имеют доступ к Docker Hub:

`docker.io/alpine:3.19`

Если кластер закрытый и образы используются только из внутреннего registry:

`registry.company.local/base/alpine:3.19`

Образ должен содержать команду:

`nsenter`

---

## 3. Если используется private registry

Node Shell Pod создаётся в namespace:

`kube-system`

Поэтому imagePullSecret должен находиться именно там.

Пример:

```bash
kubectl -n kube-system create secret docker-registry openlens-registry \
  --docker-server=registry.company.local \
  --docker-username='<USERNAME>' \
  --docker-password='<PASSWORD>'
```

Проверить:

```bash
kubectl -n kube-system get secret openlens-registry
```

После этого открыть:

OpenLens → Cluster Settings → Node Shell

В поле:

Image Pull Secret

указать:

`openlens-registry`

---

## 4. Проверить права Kubernetes

Использовать тот же kubeconfig/context, который использует OpenLens.

Проверить Nodes:

```bash
kubectl get nodes
```

Далее:

```bash
kubectl auth can-i get nodes
kubectl auth can-i list nodes
kubectl auth can-i watch nodes
```

Ожидается:

`yes`

Проверить возможность создания Node Shell Pod:

```bash
kubectl auth can-i create pods -n kube-system
kubectl auth can-i get pods -n kube-system
kubectl auth can-i list pods -n kube-system
kubectl auth can-i watch pods -n kube-system
kubectl auth can-i delete pods -n kube-system
```

Проверить подключение к shell:

```bash
kubectl auth can-i create pods/exec -n kube-system
```

Критично получить:

`yes`

для:

- create pods
- get pods
- create pods/exec

---

## 5. Проверить Node Shell в OpenLens

Перед нажатием Shell открыть терминал:

```bash
kubectl get pods -n kube-system -w
```

Теперь в OpenLens:

Nodes → worker-node → Shell

При нормальной работе должен появиться временный Pod:

`node-shell-xxxxxxxx`

Он должен перейти:

Pending → Running

После этого OpenLens должен открыть терминал worker-ноды.

---

## Если OpenLens висит на Connecting...

## 6. Проверить, создаётся ли node-shell Pod

Выполнить:

```bash
kubectl get pods -n kube-system | grep node-shell
```

### Если Pod вообще не появился

Проверить:

```bash
kubectl auth can-i create pods -n kube-system
```

Если:

`no`

проблема в RBAC.

Если:

`yes`

посмотреть события:

```bash
kubectl get events -n kube-system \
  --sort-by=.lastTimestamp | tail -50
```

Также открыть:

OpenLens → Developer Tools → Console

Искать:

- Forbidden
- admission denied
- PodSecurity
- failed to create node pod

---

## 7. Если node-shell Pod находится в Pending

Найти его:

```bash
kubectl get pods -n kube-system | grep node-shell
```

Затем:

```bash
kubectl describe pod -n kube-system <NODE-SHELL-POD>
```

Смотреть блок:

Events

---

### Если ImagePullBackOff

Проверить image:

```bash
kubectl get pod -n kube-system <NODE-SHELL-POD> \
  -o jsonpath='{.spec.containers[0].image}{"\n"}'
```

Если OpenLens пытается использовать:

`docker.io/alpine:3.19`

а worker-ноды не имеют Internet, указать внутренний image:

`registry.company.local/base/alpine:3.19`

в:

Cluster Settings → Node Shell → Node Shell Image

Если registry требует авторизацию — настроить Image Pull Secret.

---

## 8. Если Pod блокирует Pod Security

Node Shell использует privileged Pod.

Проверить:

```bash
kubectl get namespace kube-system --show-labels
```

Также посмотреть:

```bash
kubectl get events -n kube-system \
  --sort-by=.lastTimestamp | tail -50
```

Если есть сообщения вида:

- violates PodSecurity
- privileged container is not allowed
- hostPID is not allowed
- hostNetwork is not allowed

Node Shell блокируется security policy кластера.

Для Node Shell должны быть разрешены административные Pods с:

- privileged: true
- hostPID: true
- hostIPC: true
- hostNetwork: true

Не нужно отключать Pod Security всего кластера.

Нужно разрешить этот сценарий для административного доступа согласно политике конкретного Kubernetes-кластера.

---

## Pod Running, но OpenLens всё ещё пишет Connecting

## 9. Проверить статус

```bash
kubectl get pods -n kube-system | grep node-shell
```

Если Pod:

`Running`

значит:

- scheduler работает
- image скачался
- worker доступен
- Pod запустился

Теперь проверить, можно ли подключиться к Pod:

```bash
kubectl exec -it -n kube-system <NODE-SHELL-POD> \
  -c shell -- sh
```

Если shell открывается, Kubernetes-часть работает.

---

## 10. Проверить доступ именно к worker-node

Выполнить:

```bash
kubectl exec -it -n kube-system <NODE-SHELL-POD> \
  -c shell -- \
  nsenter -t 1 -m -u -i -n sh
```

После этого:

```bash
hostname
cat /etc/os-release
ip addr
ps aux | head
```

`hostname` должен соответствовать выбранной worker-node.

Если это работает, значит Kubernetes уже способен открыть shell worker-ноды.

Проблема остаётся только в OpenLens.

---

## 11. Если Kubernetes работает, а OpenLens продолжает Connecting...

Есть известный баг OpenLens/Lens, при котором:

- Node Shell Pod успешно создаётся
- Pod = Running
- `kubectl exec` работает

но вкладка OpenLens остаётся:

`Connecting...`

В таком случае открыть:

OpenLens → Developer Tools → Console

Искать ошибки:

- SHELL-SESSION
- failed to open a node shell
- TLS
- CERT
- ECONNRESET
- socket hang up
- createTerminalTab
- xterm

Если `node-shell-*` находится в Running и ручной `kubectl exec` работает, Kubernetes менять больше не нужно.

---

## 12. Рекомендуемая версия OpenLens

OpenLens является старым проектом и больше не получает полноценные upstream-обновления.

Последняя опубликованная линия бинарных сборок OpenLens:

`6.5.2`

Для неё используется extension:

`@alebcay/openlens-node-pod-menu`

Последняя версия extension:

`0.1.2`

Если текущая установка сильно отличается, рекомендуется проверить:

OpenLens → About

и:

Extensions → @alebcay/openlens-node-pod-menu

---

## 13. Быстрая диагностика

После нажатия:

Nodes → worker → Shell

### Pod не появился

Проверять:

- extension
- RBAC
- admission
- Pod Security

### Pod появился, но Pending

Проверять:

- ImagePullBackOff
- private registry
- imagePullSecret
- Pod Security
- Node scheduling

### Pod Running, но OpenLens Connecting

Проверить:

```bash
kubectl exec -it -n kube-system <NODE-SHELL-POD> \
  -c shell -- sh
```

Если работает:

- кластер исправен
- проблема OpenLens/extension

---

## 14. Итоговый рабочий сценарий

После настройки должно работать так:

```
OpenLens
   ↓
Nodes
   ↓
worker-01
   ↓
Shell
   ↓
создаётся node-shell-* в kube-system
   ↓
Pod запускается на worker-01
   ↓
OpenLens подключается к Pod
   ↓
nsenter
   ↓
terminal worker-01
```

После открытия терминала:

```bash
hostname
```

должен возвращать имя выбранной worker-ноды.

---

## 15. Чек-лист

- [ ] `@alebcay/openlens-node-pod-menu` установлен
- [ ] extension Enabled
- [ ] OpenLens перезапущен
- [ ] у Node появился пункт Shell
- [ ] Node Shell Image доступен worker-нодам
- [ ] private registry secret существует в kube-system
- [ ] Image Pull Secret указан в OpenLens
- [ ] `kubectl auth can-i create pods -n kube-system` = yes
- [ ] `kubectl auth can-i create pods/exec -n kube-system` = yes
- [ ] `node-shell-*` создаётся после нажатия Shell
- [ ] `node-shell-*` = Running
- [ ] Pod находится на выбранной worker-node
- [ ] `kubectl exec` в node-shell работает
- [ ] OpenLens перестал показывать Connecting
- [ ] `hostname` внутри OpenLens shell = имя worker-node

---

Ключевые моменты:

- OpenLens действительно рекомендует этот extension для возвращения Node/Pod menu.
- Настройки Lens/OpenLens Node Shell предусматривают отдельные Node Shell Image и Image Pull Secret из kube-system.
- Зафиксирован именно баг, где node-shell Pod уже работает, а вкладка остаётся на `Connecting...`.
- Сам OpenLens сейчас уже legacy — публичный build-репозиторий прямо пишет, что дальнейших upstream-обновлений ожидать не стоит; последняя опубликованная линия — `6.5.2`, а у extension последняя версия — `0.1.2`.
```

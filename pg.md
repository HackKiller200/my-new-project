# PostgreSQL 16 Alpine — Helm chart для Kubernetes и Argo CD

**Назначение:** развернуть **один экземпляр PostgreSQL 16 Alpine** в **том же namespace, где работает приложение**, с TLS, постоянным хранилищем, проверками здоровья и ежедневным логическим резервным копированием. Развёртывание происходит через существующий GitOps-процесс / Argo CD. Исходное приложение, его Dockerfile, Deployment и Kubernetes Service остаются отдельными от БД.

> **Статус:** конфигурация подготовлена под параметры ниже. Это **не гарантия 100% запуска в любом кластере**: для успешного запуска необходимы доступный Docker-образ, подходящий StorageClass/PV, Secret с TLS-сертификатом, разрешённые сетевые подключения и корректное использование переменных в приложении. В этом README приведены проверки и исправления для каждого условия.
>
> **Безопасность:** пароль `postgres` оставлен **строго по исходному заданию**. Такой пароль допустим только для учебного или изолированного тестового стенда. **Не публикуйте реальные пароли в GitHub**; для production используйте `auth.existingSecret`.

## Оглавление

1. [Что получим и как это связано с приложением](#1-что-получим-и-как-это-связано-с-приложением)
2. [Параметры подключения](#2-параметры-подключения)
3. [Структура chart](#3-структура-chart)
4. [Какая настройка в каком файле](#4-какая-настройка-в-каком-файле)
5. [Полный values.yaml и его ограничения](#5-полный-valuesyaml-и-его-ограничения)
6. [Предварительные проверки кластера](#6-предварительные-проверки-кластера)
7. [TLS: сертификат, Secret, проверка](#7-tls-сертификат-secret-проверка)
8. [StorageClass, PVC и сохранность данных](#8-storageclass-pvc-и-сохранность-данных)
9. [Подключение через Argo CD](#9-подключение-через-argo-cd)
10. [Что изменить в существующем Deployment приложения](#10-что-изменить-в-существующем-deployment-приложения)
11. [Проверка после синхронизации](#11-проверка-после-синхронизации)
12. [Бэкапы: расписание, запуск и проверка](#12-бэкапы-расписание-запуск-и-проверка)
13. [Восстановление из резервной копии](#13-восстановление-из-резервной-копии)
14. [Перенос данных из Docker Compose](#14-перенос-данных-из-docker-compose)
15. [Обновления и опасные изменения](#15-обновления-и-опасные-изменения)
16. [Диагностика: симптом → причина → действие](#16-диагностика-симптом--причина--действие)
17. [Рабочий чек-лист и ограничения](#17-рабочий-чек-лист-и-ограничения)
18. [Официальная документация](#18-официальная-документация)

---

## 1. Что получим и как это связано с приложением

```text
                 Один Kubernetes namespace: <YOUR_NAMESPACE>

    ┌─────────────────────────────┐
    │ Существующий Deployment     │
    │ приложения                  │
    │ POSTGRES_HOST=postgres      │
    │ POSTGRES_PORT=5432          │
    └──────────────┬──────────────┘
                   │ TCP + PostgreSQL TLS
                   ▼
    ┌─────────────────────────────┐
    │ Service postgres            │   ClusterIP, port 5432
    └──────────────┬──────────────┘
                   │
                   ▼
    ┌─────────────────────────────┐
    │ StatefulSet postgres        │
    │ Pod postgres-0              │   postgres:16-alpine
    │ DB: gay / User: postgres    │
    └──────────────┬──────────────┘
                   │
                   ▼
    ┌─────────────────────────────┐
    │ PVC data-postgres-0         │   10Gi, база хранится здесь
    └─────────────────────────────┘

    CronJob postgres-backup ──────> PVC postgres-backup (20Gi)
         ежедневно 02:00 UTC        файлы gay-<UTC_TIMESTAMP>.dump
```

Chart создаёт следующие **Kubernetes-ресурсы**:

| Ресурс | Имя | Зачем | Важное замечание |
|---|---|---|---|
| `StatefulSet` | `postgres` | Запуск PostgreSQL (1 Pod) | **Не Deployment**: один экземпляр БД с устойчивой идентичностью |
| `Pod` | `postgres-0` | Сам PostgreSQL | Создаётся StatefulSet, не отдельным YAML |
| `Service` | `postgres` | Адрес подключения для приложения | `postgres:5432` внутри одного namespace |
| `Service` | `postgres-headless` | Headless Service для StatefulSet | Не указывать как обычный `POSTGRES_HOST` |
| `Secret` | `postgres-auth` | Пароль БД | Chart создаёт при `auth.existingSecret: ""` |
| `ConfigMap` | `postgres-hba` | `pg_hba.conf` и политика TLS/SCRAM | Внешние TCP-соединения — с паролем |
| `PVC` | `data-postgres-0` | Файлы работающей БД | Создаётся из `volumeClaimTemplates`, 10Gi |
| `PVC` | `postgres-backup` | Дампы БД | 20Gi, отдельный том |
| `CronJob` | `postgres-backup` | Выполнение `pg_dump` | Каждый день по расписанию |

Отдельный `Ingress` или `LoadBalancer` **не нужен**. В этой архитектуре БД доступна **только внутри Kubernetes-кластера через ClusterIP**, если иные средства сетевого доступа не настроены.

**Что НЕ требуется делать:** создавать ещё один Deployment приложения, менять HTTP Service приложения, менять Ingress приложения или пробрасывать PostgreSQL наружу. Нужно создать **отдельное Argo CD Application для PostgreSQL** и изменить настройки подключения в **уже существующем** приложении.

## 2. Параметры подключения

| Параметр | Значение | Где задаётся |
|---|---|---|
| Образ | `postgres:16-alpine` | `values.yaml → image` |
| Kubernetes namespace | **тот же, что у приложения** | `Argo CD Application → spec.destination.namespace`, не `values.yaml` |
| Имя Service / DB host | `postgres` | `fullnameOverride: postgres`; в Deployment приложения: `POSTGRES_HOST=postgres` |
| Порт | `5432` | `values.yaml → service.port`; в Deployment: `POSTGRES_PORT=5432` |
| Пользователь | `postgres` | `values.yaml → auth.username` |
| Пароль | `postgres` | `values.yaml → auth.password` или внешний Secret |
| База данных | `gay` | `values.yaml → auth.database` |
| TLS | включён, обязателен для TCP | `tls.enabled: true`, `tls.require: true` |
| DNS в другом namespace | `postgres.<namespace>.svc.cluster.local` | Только если приложение **не** в том же namespace |

**Почему `localhost` неверен:** внутри Pod `localhost` указывает на **сам Pod**, а не на соседний Pod с PostgreSQL. Для приложения используется `postgres` — DNS Service. `localhost` допустим при диагностике **изнутри самого Pod `postgres-0`**.

## 3. Структура chart

```text
postgresql-helm/
├── Chart.yaml                    # Метаданные Helm
├── values.yaml                   # Все текущие значения
├── values.schema.json            # Проверка типов/границ значений в Helm
├── .helmignore
├── README.md                     # Эта инструкция
└── templates/
    ├── _helpers.tpl              # Имена/labels
    ├── secret.yaml               # Secret postgres-auth
    ├── services.yaml             # postgres и postgres-headless
    ├── configmap-hba.yaml        # pg_hba.conf, TLS/SCRAM
    ├── statefulset.yaml          # PostgreSQL, PVC, TLS, пробы, ресурсы
    ├── backup-pvc.yaml           # PVC postgres-backup
    └── backup-cronjob.yaml       # CronJob pg_dump
```

## 4. Какая настройка в каком файле

**Правило:** настройки БД редактируйте **в `values.yaml`**, а не прямо в `templates/*.yaml`.

| Что хотите изменить | Где править | Что произойдёт |
|---|---|---|
| Образ / tag | `image.repository`, `image.tag` | Новый образ при пересоздании Pod; major upgrade требует отдельной процедуры |
| Имя Kubernetes Service | `fullnameOverride` | **Опасно после первого запуска**: меняются имена ресурсов и PVC |
| Порт | `service.port` | Меняется Service и параметр запуска PostgreSQL |
| Пользователь, БД, пароль на пустом PVC | `auth.*` | Срабатывает **при первоначальном `initdb`**, а не на существующей БД |
| Использовать заранее созданный пароль | `auth.existingSecret` | Chart перестаёт создавать `postgres-auth` и читает Secret указанного имени |
| Размер БД на новом PVC | `persistence.size` | Размер нового PVC; изменение размера уже созданного PVC — особый случай |
| StorageClass БД | `persistence.storageClass` | Выбор StorageClass при создании PVC |
| TLS / обязательность | `tls.enabled`, `tls.require`, `tls.existingSecret` | Сертификат из Kubernetes Secret; `pg_hba.conf` управляет TCP-подключениями |
| Подключения/память PostgreSQL | `postgresql.*` | Аргументы `postgres -c ...` в StatefulSet |
| Requests / Limits | `resources.*` | Ресурсы основного контейнера |
| Пробы | `probes.*` | Старт / готовность / живость PostgreSQL |
| Периодичность резервного копирования | `backup.schedule`, `backup.timeZone` | Расписание CronJob |
| Время хранения бэкапов | `backup.retentionDays` | Удаление старых `*.dump` при очередном успешном запуске скрипта |
| PVC резервных копий | `backup.size`, `backup.storageClass` | Хранилище для дампов |
| Ограничения Job | `backup.resources`, `backup.activeDeadlineSeconds` | Ресурсы и максимальное время выполнения backup Job |
| Namespace | **Argo CD**, не chart | Куда Kubernetes создаст ресурсы |
| `POSTGRES_HOST` приложения | **Deployment / Helm values приложения** | DNS-имя существующего Service `postgres` |
| HTTP Service / Ingress приложения | **не изменять** | Не участвуют в подключении к БД |

`Chart.yaml` — метаданные (`name`, `version`, `appVersion`), там **не надо писать** `POSTGRES_DB`, `POSTGRES_USER`, пароль или namespace.

## 5. Полный values.yaml и его ограничения

Файл **уже приложен к chart**. Ниже — актуальная конфигурация с твоими параметрами. Комментарии в файле объясняют, что можно редактировать.

```yaml
fullnameOverride: postgres

image:
  repository: postgres
  tag: "16-alpine"
  pullPolicy: IfNotPresent

service:
  port: 5432

auth:
  username: postgres
  password: "postgres"   # ТОЛЬКО ТЕСТОВЫЙ СТЕНД
  database: gay
  existingSecret: ""

persistence:
  size: 10Gi
  storageClass: ""      # default StorageClass кластера
  accessModes:
    - ReadWriteOnce

tls:
  enabled: true
  require: true
  existingSecret: postgres-tls
  minVersion: TLSv1.2

postgresql:
  maxConnections: 100
  sharedBuffers: 128MB
  effectiveCacheSize: 384MB
  logMinDurationStatement: -1

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: "1"
    memory: 1Gi

probes:
  startup:
    periodSeconds: 5
    timeoutSeconds: 3
    failureThreshold: 60
  readiness:
    periodSeconds: 10
    timeoutSeconds: 3
    failureThreshold: 3
  liveness:
    periodSeconds: 20
    timeoutSeconds: 3
    failureThreshold: 3

backup:
  enabled: true
  schedule: "0 2 * * *"
  timeZone: "Etc/UTC"
  retentionDays: 7
  size: 20Gi
  storageClass: ""
  accessModes:
    - ReadWriteOnce
  backoffLimit: 2
  activeDeadlineSeconds: 3600
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 2
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      cpu: 500m
      memory: 512Mi
```

**Выбранные значения не универсальны:** `10Gi/20Gi`, `256Mi/1Gi`, `cpu: 1` — разумный старт для небольшого стенда; реальные запросы, размер таблиц, сложность запросов, нагрузка, особенности CSI и выделенные ресурсы узлов неизвестны. При OOMKill увеличить память/оптимизировать PostgreSQL; при заполнении томов — расширять хранилище в соответствии с возможностями StorageClass.

Файл `values.schema.json` проверяет обязательные секции, типы и некоторые границы (`retentionDays >= 1`, корректный порт и т. п.) при запуске Helm. Он **не проверяет существование PVC, сертификатов и возможности кластера**.

### Настройки TLS — три режима

| Настройка | Результат |
|---|---|
| `enabled: true`, `require: true` | **Текущий вариант:** TCP только с TLS, аутентификация SCRAM |
| `enabled: true`, `require: false` | TLS доступен, но не обязателен для TCP; не рекомендуется вне теста |
| `enabled: false`, `require: false` | TLS выключен, TCP с SCRAM без шифрования |

Комбинация `enabled: false`, `require: true` запрещена шаблоном. **Если `tls.enabled: true`, Secret `postgres-tls` обязателен.**

### Отдельно: значения в `Deployment` приложения

Их **нет** в этом `values.yaml`, потому что они относятся к *другому* chart/Deployment. Они показаны в [разделе 10](#10-что-изменить-в-существующем-deployment-приложения).

---

## 6. Предварительные проверки кластера

Во всех командах замените `my-namespace` на **реальный namespace приложения**:

```bash
export NS=my-namespace

kubectl config current-context
kubectl get namespace "$NS"
kubectl -n "$NS" get deployment
kubectl get storageclass
kubectl get nodes
kubectl version --client
```

Проверить также версию **сервера** Kubernetes:

```bash
kubectl version
```

Требования:

1. Kubernetes **1.27+** — для поля `CronJob.spec.timeZone`. Если версия старее, задайте `backup.timeZone: ""`, чтобы chart не рендерил это поле (точная часовая зона выполнения тогда зависит от конфигурации controller-manager).
2. Рабочий **StorageClass** с динамическим выделением томов, либо заранее созданные совместимые PV. Для `ReadWriteOnce` один том может быть смонтирован для записи одним узлом за раз (не обязательно одним Pod на том же узле).
3. Образ `postgres:16-alpine` доступен на рабочих узлах через Интернет/внутренний registry/cache. Для offline-кластера предварительно загрузить образ и при необходимости поменять `image.repository`.
4. Политики безопасности namespace разрешают запуск официального образа, в том числе root на стадии настройки прав. Строгий **Pod Security Restricted** может запретить текущие контейнеры — это отдельная адаптация securityContext/volume permissions.
5. Достаточно ресурсов на нодах для основного Pod и backup Job.
6. Есть права у Argo CD создавать StatefulSet, Service, ConfigMap, Secret, PVC, CronJob в этом namespace.
7. Нет уже существующих объектов `postgres`, `postgres-auth`, `postgres-backup` и `data-postgres-0` с конфликтующей конфигурацией/данными (при наличии нужно разбираться, а не удалять).

Проверить имена и доступные ресурсы:

```bash
kubectl -n "$NS" get sts,svc,cm,secret,pvc,cronjob | grep postgres || true
kubectl -n "$NS" get events --sort-by=.metadata.creationTimestamp | tail -n 30
```

**Если есть старый PVC:** **не удаляйте** его вслепую. Установленный поверх старых файлов PostgreSQL может увидеть чужой кластер/версию/пароль/имя базы. Сначала оцените происхождение данных и план миграции.

## 7. TLS: сертификат, Secret, проверка

### 7.1. Что должен содержать Secret

`values.yaml`:

```yaml
tls:
  enabled: true
  require: true
  existingSecret: postgres-tls
  minVersion: TLSv1.2
```

В **том же namespace** нужен Kubernetes Secret `postgres-tls` с ключами:

- `tls.crt` — сертификат сервера (или цепочка, если применимо);
- `tls.key` — соответствующий приватный ключ.

**Chart не выпускает сертификаты.** Если TLS Secret ещё нет, `Pod` не сможет смонтировать том `tls-source` и не запустится.

### 7.2. Если сертификат уже выдан

```bash
kubectl -n "$NS" create secret tls postgres-tls \
  --cert=./tls.crt \
  --key=./tls.key

kubectl -n "$NS" get secret postgres-tls
```

Если Secret уже существует и управляется cert-manager / External Secrets, не создавайте второй. Укажите его имя в `tls.existingSecret`.

### 7.3. Самоподписанный сертификат для теста

**Только для лабораторного контура:**

```bash
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout tls.key -out tls.crt \
  -subj '/CN=postgres' \
  -addext "subjectAltName=DNS:postgres,DNS:postgres.${NS}.svc,DNS:postgres.${NS}.svc.cluster.local"

kubectl -n "$NS" create secret tls postgres-tls \
  --cert=./tls.crt --key=./tls.key
```

Если OpenSSL старый и не понимает `-addext`, нужна версия с поддержкой этого ключа либо SAN через config-файл. Не коммитьте `tls.key` в Git.

**Права на ключ:** `initContainer prepare-tls` копирует его из Secret в `emptyDir`, назначает владельца `postgres` и права `0600`. Это необходимо, потому что PostgreSQL предъявляет строгие требования к приватному ключу. На `prepare-tls` требуется возможность запуска root; если запрещено Pod Security Policy/Admission — придётся менять способ подготовки ключа.

### 7.4. Проверка TLS после запуска

```bash
kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay -c 'SHOW ssl;'
```

Ожидаемо: `on`. Далее проверяем **реальное TCP-соединение**, не Unix socket:

```bash
kubectl -n "$NS" exec postgres-0 -- sh -ec '
  PGSSLMODE=require PGPASSWORD="$POSTGRES_PASSWORD" \
  psql -h 127.0.0.1 -p "$PGPORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -Atc "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid();"
'
```

Ожидаемо: `t` (`true`). Здесь `127.0.0.1` **правильно**: команда выполняется внутри самого Pod БД. В Deployment другого приложения остаётся `POSTGRES_HOST=postgres`.

Проверка запрета незашифрованного TCP:

```bash
kubectl -n "$NS" exec postgres-0 -- sh -ec '
  PGSSLMODE=disable PGPASSWORD="$POSTGRES_PASSWORD" \
  psql -h 127.0.0.1 -p "$PGPORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1"
'
```

**Ожидается ошибка**, потому что `tls.require: true`. Эта команда — **негативный тест**, а не признак неисправности.

`PGSSLMODE=require` **шифрует трафик**, но само по себе обычно не обеспечивает полноценную проверку личности сервера. Для production настройте доверенный CA у клиента и режим `verify-full` (либо эквивалент драйвера). В сертификате должны присутствовать DNS-имена, реально используемые приложением.

### 7.5. Если TLS не нужен на тестовом стенде

Измените **обе** настройки:

```yaml
tls:
  enabled: false
  require: false
  existingSecret: ""
  minVersion: TLSv1.2
```

В Deployment приложения также уберите SSL-режим или задайте соответствующую настройку драйвера (`sslmode=disable` для libpq). Это **не рекомендовано для production**, но позволяет отделить проблемы сети/БД от проблем сертификатов.

---

## 8. StorageClass, PVC и сохранность данных

Посмотреть StorageClass:

```bash
kubectl get storageclass
```

Если есть класс, помеченный `(default)`, текущие значения:

```yaml
persistence:
  size: 10Gi
  storageClass: ""
  accessModes: [ReadWriteOnce]
backup:
  size: 20Gi
  storageClass: ""
  accessModes: [ReadWriteOnce]
```

позволяют Kubernetes использовать default StorageClass при создании PVC.

Если класса по умолчанию **нет**, задайте реальный класс (например, `local-path`, `longhorn`, `nfs-csi` или другой **только если он существует у вас**):

```yaml
persistence:
  storageClass: "<ACTUAL_STORAGE_CLASS>"
backup:
  storageClass: "<ACTUAL_STORAGE_CLASS>"
```

Проверить PVC после установки:

```bash
kubectl -n "$NS" get pvc
kubectl -n "$NS" describe pvc data-postgres-0
kubectl -n "$NS" describe pvc postgres-backup
```

Состояние обоих PVC должно стать `Bound`. Для StorageClass с `WaitForFirstConsumer` PVC может оставаться `Pending` до назначения Pod узлу — оценивайте события `kubectl describe`.

**Ключевые ограничения:**

- `data-postgres-0` — основной диск с PostgreSQL (`PGDATA` хранится во вложенном каталоге `pgdata`, чтобы избежать `lost+found` на корне некоторых файловых систем).
- `postgres-backup` — отдельный PVC. Он защищён аннотацией Argo CD `Prune=false,Delete=false` от стандартного автоматического удаления. При отключении бэкапов PVC намеренно остаётся, его удаляют **вручную и осознанно**.
- `StatefulSet` по умолчанию **сохраняет** PVC `data-postgres-0` при удалении StatefulSet. Это не гарантия восстановления после повреждения самого PV/хранилища.
- Удаление `PVC` может приводить к удалению реального диска в зависимости от `reclaimPolicy` соответствующего PV. **Не удаляйте PVC для «перезапуска» БД.**
- Изменение `persistence.size`, `persistence.storageClass`, `accessModes` после создания `volumeClaimTemplates` часто невозможно обычным `argocd sync`: шаблон StatefulSet частично **immutable**. Увеличение размеров делается отдельной процедурой, зависящей от CSI/StorageClass.
- `ReadWriteOnce` с существующим PVC может приводить к `Multi-Attach` при попытке смонтировать диск на другой узел, пока он подключён к первому.
- Backup PVC и DB PVC могут физически находиться на **одном storage backend**. Это **не disaster recovery**. Для боевой БД необходима копия **вне кластера/в другом failure domain**.

## 9. Подключение через Argo CD

### 9.1. Сначала проверить chart локально

Из корня репозитория, где директория `postgresql-helm` находится рядом:

```bash
helm lint ./postgresql-helm
helm template postgres ./postgresql-helm \
  --namespace "$NS" \
  --values ./postgresql-helm/values.yaml \
  > /tmp/postgres-rendered.yaml

# Убедиться, что ресурсы и имена получились ожидаемыми:
grep -E '^kind:|^  name:' /tmp/postgres-rendered.yaml
```

`helm template` не обращается к кластеру и **не проверяет**, существует ли TLS Secret/StorageClass.

**Запускать дополнительно `helm upgrade --install` в тот же namespace не надо**, если chart уже управляется Argo CD — иначе получите конкурирующие системы управления одним набором ресурсов.

### 9.2. Пример Argo CD Application

**Это пример; измените URL Git-репозитория, путь и namespace.** Сам манифест Argo CD Application находится обычно в namespace `argocd` (или в том, где установлен Argo CD). Его **`spec.destination.namespace`** должен совпадать с namespace приложения.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: postgres
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/ORG/REPO.git
    targetRevision: main
    path: postgresql-helm
    helm:
      releaseName: postgres
      valueFiles:
        - values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: my-namespace # ЗАМЕНИТЬ на namespace существующего приложения
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

**Почему `metadata.name` и `releaseName` оба `postgres`:** при стандартном режиме tracking labels Argo CD может выставлять собственный `app.kubernetes.io/instance`; несовпадение Application name и release name иногда создаёт проблемы с selectors chart. В предложенной конфигурации они совпадают.

Если ваш проект уже использует **ApplicationSet**, **app-of-apps** или существующий пайплайн, **не создавайте дублирующий Application**. Добавьте путь к этому chart существующим способом, сохранив правильные namespace и release name.

### 9.3. Порядок GitOps-развёртывания

1. Подтвердить существование namespace, StorageClass, доступность образа.
2. Создать `postgres-tls` в **целевом namespace** (или настроить его создание через безопасный GitOps/Secret controller).
3. Закоммитить папку `postgresql-helm` в репозиторий, на который смотрит Argo CD.
4. Запустить синхронизацию Argo CD Application БД.
5. Дождаться `postgres-0 Ready` и проверки SQL/TLS.
6. **Только после этого** поменять переменные в Deployment приложения и синхронизировать Deployment.
7. Убедиться, что приложение устанавливает соединение с PostgreSQL.
8. Вручную запустить проверочный backup Job и убедиться, что дамп создаётся.

**Не менять Deployment приложения раньше**, если в нём нет retry подключения к БД: приложение может уйти в CrashLoop до готовности PostgreSQL.

### 9.4. Предупреждения по Argo CD

- Chart для PostgreSQL и chart приложения могут находиться в разных репозиториях и быть разными Argo CD Application; важно лишь, чтобы ресурсы БД и приложения находились в нужном namespace и использовались верные параметры.
- **Не делайте** автоматический `Force=true` / `Replace=true` «чтобы исправить StatefulSet»: это может вызвать разрушительные изменения. При immutable-полях PVC нужен отдельно спланированный путь миграции.
- При выключении `backup.enabled` CronJob удалится, **PVC с дампами сохранится** из-за `Prune=false,Delete=false`; Argo может показывать объект как `OutOfSync`/требующий prune — это ожидаемо, не удаляйте его не разобравшись.
- Секрет, созданный **вручную**, должен продолжать существовать; его наличие **не обеспечивает** сам Helm, если выбран `existingSecret`.

---

## 10. Что изменить в существующем Deployment приложения

В Helm chart **приложения** найдите шаблон `Deployment` и секцию нужного контейнера `spec.template.spec.containers[].env` или аналогичные параметры его `values.yaml`.

Пример фрагмента **готового Kubernetes Deployment**:

```yaml
spec:
  template:
    spec:
      containers:
        - name: application  # имя здесь должно совпасть с вашим реальным контейнером
          # image: ...       # ваш существующий образ — НЕ менять
          env:
            - name: POSTGRES_HOST
              value: "postgres"
            - name: POSTGRES_PORT
              value: "5432"
            - name: POSTGRES_USER
              value: "postgres"
            - name: POSTGRES_DB
              value: "gay"
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: postgres-auth
                  key: postgres-password
            - name: PGSSLMODE
              value: "require"
```

Это **фрагмент**, а не целиком рабочий Deployment (существующие `metadata`, `selector`, образ, порты и т. п. должны остаться).

**Критически важно:** приложение может ожидать переменные **не** `POSTGRES_*`, а `DB_HOST`, `DB_NAME`, `DATABASE_URL`, `PGHOST`, `PGDATABASE`, `PGUSER` и т. д. Сверьтесь со старым `docker-compose.yml` и кодом. **Названия `POSTGRES_*` не стандартизируют автоматически настройки приложения.** Важно именно то, какие имена читает код.

Пример единой строки подключения для драйвера, который принимает libpq-формат:

```text
postgresql://postgres:postgres@postgres:5432/gay?sslmode=require
```

**В GitHub такой URL с реальным паролем не помещайте.** Пароль передавайте через Kubernetes Secret. Если ваш ORM работает через `DATABASE_URL`, создавайте его в безопасном Secret или в механизме конфигурации приложения; не следует автоматически предполагать, что все драйверы читают `PGSSLMODE`.

**Проверить:**

```bash
kubectl -n "$NS" get deployment
kubectl -n "$NS" describe deployment <APP_DEPLOYMENT_NAME>
kubectl -n "$NS" logs deployment/<APP_DEPLOYMENT_NAME> --tail=100
```

Если есть NetworkPolicy в namespace, разрешите **TCP 5432** от Pod приложения к Pod с метками PostgreSQL. DNS (обычно UDP/TCP 53) также должен работать.

Не надо изменять Service, который обслуживает HTTP/HTTPS-трафик самого приложения. **Service `postgres` — отдельный сервис БД.**

## 11. Проверка после синхронизации

### 11.1. Быстрая сводка

```bash
kubectl -n "$NS" get sts,pods,svc,pvc,cronjob
kubectl -n "$NS" get secret postgres-auth postgres-tls
```

Ожидаемые имена:

```text
statefulset.apps/postgres
pod/postgres-0                         (Running, 1/1 Ready)
service/postgres                       (ClusterIP, port 5432)
service/postgres-headless              (ClusterIP None)
persistentvolumeclaim/data-postgres-0  (Bound)
persistentvolumeclaim/postgres-backup  (Bound)
cronjob.batch/postgres-backup
```

### 11.2. SQL-проверка: пользователь, база, сервер

```bash
kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay -c 'SELECT current_database(), current_user, version();'

kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay -c 'SHOW port;'

kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay -c 'SHOW ssl;'
```

Должны быть `gay`, `postgres`, PostgreSQL 16.x, порт `5432`, `ssl=on`.

**Ограничение:** `psql` без `-h` проверяет локальный Unix socket, а не сетевой доступ приложения. Для проверки TCP+пароля+TLS используйте [раздел 7.4](#74-проверка-tls-после-запуска).

### 11.3. Service и Endpoints

```bash
kubectl -n "$NS" get svc postgres -o wide
kubectl -n "$NS" get endpointslice -l kubernetes.io/service-name=postgres
kubectl -n "$NS" get pods --show-labels
```

Если Service существует, но Endpoints пусты, проверьте `postgres-0 Ready`, selectors Service, readinessProbe, labels Pod.

### 11.4. Логи и события

```bash
kubectl -n "$NS" logs postgres-0 -c postgres --tail=150
kubectl -n "$NS" logs postgres-0 -c prepare-tls --tail=100
kubectl -n "$NS" describe pod postgres-0
kubectl -n "$NS" get events --sort-by=.metadata.creationTimestamp | tail -n 40
```

Если контейнер перезапускался:

```bash
kubectl -n "$NS" logs postgres-0 -c postgres --previous --tail=150
```

### 11.5. Проверить реальные параметры из PostgreSQL

```bash
kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay -c 'SHOW max_connections;'

kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay -c 'SHOW shared_buffers;'
```

**Пробы `pg_isready`:** проверяют, принимает ли сервер подключения; **не доказывают** корректность пароля пользователя, существование приложения или валидность TLS-сертификата у внешнего клиента. Поэтому нужны отдельные проверки выше.

## 12. Бэкапы: расписание, запуск и проверка

В `values.yaml`:

```yaml
backup:
  enabled: true
  schedule: "0 2 * * *"
  timeZone: "Etc/UTC"
  retentionDays: 7
  size: 20Gi
```

- `pg_dump -Fc` создаёт **логический** архив **только** базы `gay`.
- Расписание: ежедневно **02:00 UTC** (для Москвы это 05:00).
- Файлы: `/backups/gay-YYYYMMDDTHHMMSSZ.dump` в отдельном PVC `postgres-backup`.
- Сначала создаётся временный `.tmp` файл; после успешного `pg_dump`, непустого файла и чтения списка объектов `pg_restore --list` архив переименовывается в `.dump`.
- Старые архивы удаляются при успешном выполнении скрипта по `retentionDays` (при неработающем CronJob автоматическая очистка не происходит).
- `concurrencyPolicy: Forbid` запрещает планировщику запускать одновременно два экземпляра **этого** CronJob, но не защищает от вручную созданных параллельных Job.
- `activeDeadlineSeconds: 3600` — максимум 1 час для Job; если реальные дампы занимают дольше, увеличьте параметр.
- Нужен доступ Job к БД через `postgres:5432` и к PVC для записи.

### Ручная проверка бэкапа

```bash
export JOB="postgres-backup-test-$(date +%s)"
kubectl -n "$NS" create job "$JOB" --from=cronjob/postgres-backup
kubectl -n "$NS" wait --for=condition=complete "job/$JOB" --timeout=10m
kubectl -n "$NS" logs "job/$JOB" --tail=100
```

Ожидается строка `Backup OK: /backups/gay-....dump` и размер файла. Если Job не завершился, выполнить:

```bash
kubectl -n "$NS" describe job "$JOB"
kubectl -n "$NS" get pods -l "batch.kubernetes.io/job-name=$JOB"
kubectl -n "$NS" logs "job/$JOB" --all-containers=true --tail=150
kubectl -n "$NS" get events --sort-by=.metadata.creationTimestamp | tail -n 30
```

Если `wait` вышел по timeout, это **не обязательно** означает сбой: прочитайте статус Job и логи; для крупной БД 10 минут могут быть недостаточны.

**Важно:** основной Pod `postgres-0` **не монтирует** PVC `postgres-backup`. Поэтому команда `kubectl exec postgres-0 -- ls /backups` не работает — см. следующий раздел.

## 13. Восстановление из резервной копии

**Любое восстановление в рабочую базу потенциально разрушительно. Сначала проверьте архив в отдельной базе.** Ниже — проверяемая ручная процедура.

### 13.1. Получить файл с backup PVC

Создайте **временный** Pod с томом бэкапа (используется тот же образ, что и у БД):

```bash
cat <<'YAML' | kubectl -n "$NS" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: postgres-backup-reader
spec:
  restartPolicy: Never
  containers:
    - name: reader
      image: postgres:16-alpine
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: backups
          mountPath: /backups
          readOnly: true
  volumes:
    - name: backups
      persistentVolumeClaim:
        claimName: postgres-backup
YAML

kubectl -n "$NS" wait pod/postgres-backup-reader --for=condition=Ready --timeout=180s
kubectl -n "$NS" exec postgres-backup-reader -- ls -lh /backups
```

Выберите реальное имя `*.dump` из вывода и скачайте:

```bash
kubectl -n "$NS" cp \
  postgres-backup-reader:/backups/gay-YYYYMMDDTHHMMSSZ.dump \
  ./gay-test-restore.dump
```

`YYYYMMDDTHHMMSSZ` необходимо заменить **на реально существующее имя файла**. На медленном CSI при `ReadWriteOnce` Pod-reader может ждать переноса тома с другой ноды — смотрите `describe pod` и сообщения `Multi-Attach`.

После копирования временный Pod удалить:

```bash
kubectl -n "$NS" delete pod postgres-backup-reader
```

### 13.2. Проверить архив локально

```bash
pg_restore --list ./gay-test-restore.dump | head -n 30
```

Для этой команды на рабочей машине должен быть установлен клиент PostgreSQL (`pg_restore`) совместимой версии. Проверка списка объектов сама по себе **не гарантирует** полноту восстановления — требуется реальный пробный restore.

### 13.3. Восстановить в НОВУЮ тестовую базу

**Не используйте `gay` для проверки восстановления.** Создайте отдельную БД:

```bash
kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d postgres -c 'CREATE DATABASE gay_restore_test;'

kubectl -n "$NS" cp ./gay-test-restore.dump \
  postgres-0:/tmp/gay-test-restore.dump

kubectl -n "$NS" exec postgres-0 -- \
  pg_restore -U postgres -d gay_restore_test \
  --no-owner --no-privileges --exit-on-error \
  /tmp/gay-test-restore.dump
```

Проверить таблицы:

```bash
kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d gay_restore_test -c \
  "SELECT count(*) AS tables_count FROM pg_tables WHERE schemaname='public';"
```

Если есть ожидаемые таблицы/данные, провести прикладную проверку содержимого. Удаление тестовой БД **только после проверки**, если она больше не нужна:

```bash
kubectl -n "$NS" exec postgres-0 -- \
  psql -U postgres -d postgres -c 'DROP DATABASE gay_restore_test;'

kubectl -n "$NS" exec postgres-0 -- rm -f /tmp/gay-test-restore.dump
```

**Ограничения логического backup:** `pg_dump` **не включает глобальные роли/пользователей** кластера как `pg_dumpall --globals-only`; не содержит WAL для point-in-time recovery (PITR); не гарантирует сохранность при потере всего PVC/storage. Расписание и RPO/RTO выбирайте отдельно под требования приложения.

---

## 14. Перенос данных из Docker Compose

Если в `docker-compose` была **не пустая** база, новый Helm chart **не перенесёт её сам**. Начальная БД `gay` создастся пустой. При наличии нужных данных выполняйте перенос `pg_dump/pg_restore` с согласованным окном переключения приложения.

### 14.1. Старая среда Docker Compose

Проверить имя DB-service в старом compose, например `db`. Замените `<COMPOSE_DB_SERVICE>` на ваше имя:

```bash
docker compose ps

docker compose exec -T <COMPOSE_DB_SERVICE> \
  pg_dump -U postgres -d gay -Fc > ./gay-from-compose.dump
```

Команда выполняется на машине Docker Compose и сохраняет файл **на хосте**. Лучше сделать дамп во время остановки записей приложения или выполнить последнюю синхронизацию перед переключением, иначе новые изменения после дампа не попадут в Kubernetes.

### 14.2. Перенести файл в Pod новой БД

```bash
kubectl -n "$NS" cp ./gay-from-compose.dump \
  postgres-0:/tmp/gay-from-compose.dump
```

**На новой пустой `gay` базе**:

```bash
kubectl -n "$NS" exec postgres-0 -- \
  pg_restore -U postgres -d gay \
  --no-owner --no-privileges --exit-on-error \
  /tmp/gay-from-compose.dump
```

Если в целевой БД уже есть таблицы/данные, **не запускать эту команду повторно без плана** — возможны конфликты и дубли. Создайте backup, определите, какие объекты уже существуют, и используйте отдельную процедуру миграции/замены базы.

### 14.3. Переключить приложение

Только после проверки данных измените Deployment приложения на `POSTGRES_HOST=postgres` и выполните Argo CD sync. При работающем приложении со старой БД обеспечьте остановку записи перед финальным dump, иначе получите рассинхронизацию.

## 15. Обновления и опасные изменения

### Можно обычно изменить в values.yaml

- `resources.requests/limits` (при наличии ресурсов на нодах; Pod может перезапуститься).
- `probes` (будет rollout Pod).
- `backup.schedule`, `backup.timeZone`, `backup.retentionDays`, ресурсы бэкапа.
- `postgresql.maxConnections` и прочие `postgresql.*` (перезапуск PostgreSQL; проверяйте память).
- TLS-сертификат в Kubernetes Secret, **но** требуется перезапустить Pod, потому что initContainer копирует сертификат один раз при старте.

Для перезапуска Pod после обновления TLS Secret (с учётом вашей GitOps-политики):

```bash
kubectl -n "$NS" rollout restart statefulset/postgres
kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s
```

При включённом `selfHeal` Argo CD может отследить изменение pod-template-аннотации от `rollout restart`. Проверьте Sync/OutOfSync после команды. **Не удаляйте PVC.**

### Нельзя просто менять без миграции / отдельной операции

| Действие | Почему опасно | Что делать |
|---|---|---|
| `auth.password` после первого запуска | Старый пароль хранится в самой БД, Secret и фактическая роль рассинхронизируются | Сначала изменить пароль SQL-командой, затем Secret и переменные приложения; проверить login |
| `auth.database` / `auth.username` после первого запуска | В уже инициализированном PGDATA новые DB/role автоматически не создаются | `CREATE DATABASE` / `CREATE ROLE` или миграция схемы вручную |
| `fullnameOverride` / Helm release labels | Изменяются имена Service/PVC/selector, может быть создан другой Pod с новой пустой базой | Сохранять `postgres` и имя Argo Application; изменение — только по плану |
| `persistence.size` у уже существующего PVC | `volumeClaimTemplates` StatefulSet не обновляются обычным способом | Процедура volume expansion конкретного CSI; проверить `allowVolumeExpansion` |
| `persistence.storageClass` | Нельзя просто переназначить существующий диск другому классу | Новый том, миграция данных, проверка |
| Переход `postgres:16-alpine` → `17-alpine` | **Major** upgrade PostgreSQL и несовместимость физического PGDATA | `pg_upgrade` или dump/restore, резервные копии и тест |
| `kubectl delete pvc data-postgres-0` | Удаление может уничтожить реальные данные | Не делать для «перезапуска» или лечения ошибок |
| Удаление Secret в работающей БД | Подключение приложения/backup Job может перестать работать | Изменять и вращать Secret контролируемо |

Для безопасной смены пароля на существующей БД используйте интерактивный `psql`:

```bash
kubectl -n "$NS" exec -it postgres-0 -- psql -U postgres -d postgres
```

Внутри `psql`:

```sql
\password postgres
```

После этого **обновите соответствующий Kubernetes Secret и перезапустите приложение**, чтобы его переменные окружения подхватили новое значение. Не рассчитывайте, что запись нового `auth.password` в values автоматически выполнит `ALTER ROLE` в PostgreSQL.

## 16. Диагностика: симптом → причина → действие

### Минимальный набор команд при любой ошибке

```bash
kubectl -n "$NS" get sts,pods,svc,pvc,cronjob,job
kubectl -n "$NS" describe pod postgres-0
kubectl -n "$NS" logs postgres-0 -c postgres --tail=200
kubectl -n "$NS" logs postgres-0 -c prepare-tls --tail=100
kubectl -n "$NS" get events --sort-by=.metadata.creationTimestamp | tail -n 50
```

Если контейнер ещё не создался, `logs` могут ответить ошибкой — **это нормально**, начните с `describe pod` и Events.

| Симптом | Вероятная причина | Где смотреть | Что исправить |
|---|---|---|---|
| `FailedMount`, `secret "postgres-tls" not found` | TLS Secret отсутствует в этом namespace | `get secret postgres-tls`, `describe pod` | Создать Secret в **целевом** namespace, проверить `tls.existingSecret` |
| `prepare-tls` не запускается / `CreateContainerConfigError` | Запрет root, политика admission, битый Secret | Events, `logs -c prepare-tls` | Проверить PodSecurity; при необходимости адаптировать chart под политики |
| `could not load private key file`, `Permission denied` | TLS ключ/права/формат сертификата | `logs -c postgres` | Проверить `tls.crt`, `tls.key`, соответствие пары и initContainer |
| `PVC Pending` | Нет StorageClass/PV, неправильный класс, нехватка ёмкости | `get storageclass`, `describe pvc ...` | Настроить реальный StorageClass/PV, проверить CSI и `WaitForFirstConsumer` |
| `Multi-Attach error` | RWO PVC уже монтируется на другой ноде | `describe pod`, события CSI | Дождаться освобождения тома; не удалять PVC |
| `ImagePullBackOff` | Нода не может скачать `postgres:16-alpine` | `describe pod`, pull secrets, registry | Загрузить образ в offline registry, исправить `image.repository` |
| `CrashLoopBackOff` после запуска | Ошибка инициализации PGDATA, прав, конфигурации или ресурсов | `logs -c postgres --previous` | Исправлять причину из **первых FATAL/ERROR** в логах, не удалять PV |
| `OOMKilled` / exit code `137` | Контейнер превысил память | `describe pod`, `resources` | Увеличить limit, снизить нагрузку, настроить `postgresql.*` |
| PostgreSQL Pod `Running`, но `0/1 Ready` | Probe не проходит / сервер ещё стартует | `describe pod`, `logs`, `pg_isready` | Проверить лог и `probes.*`; не увеличивать timeout вслепую |
| `postgres` Service без Endpoints | Pod не Ready или selector labels не совпали | `get endpointslice`, `describe svc`, pod labels | Исправить readiness или labels; согласовать имена Argo Application/Helm release |
| Приложение: `connection refused` | PostgreSQL не слушает порт, Pod не Ready, неверный host/port | Service, Endpoints, logs приложения | Проверить `POSTGRES_HOST=postgres`, порт 5432, readiness и сетевые политики |
| Приложение: `could not translate host name` | DNS/CoreDNS/namespace либо неверный host | `nslookup postgres` из debug Pod, CoreDNS | Использовать `postgres` в одном ns или полный FQDN в другом |
| Приложение: `password authentication failed` | Неверный Secret, пароль в базе не равен переменной, плохое имя роли | `auth`, Secret, логи PostgreSQL | Сверить сохранённую роль и пароль; для существующего PVC нужен `ALTER ROLE` |
| Приложение: `database "gay" does not exist` | База не была создана при initdb / существующий PVC | SQL `\l`, `POSTGRES_DB` | Создать БД отдельно после проверки данных; изменения `values` не применяются задним числом |
| Приложение: `no pg_hba.conf entry ... no encryption` | Клиент пытается подключиться **без TLS** | `tls.require`, настройки драйвера | Включить SSL в приложении (`sslmode=require` либо аналог) |
| Приложение: `certificate verify failed` | Клиент требует CA/SAN, сертификат не доверенный | Сертификат, DNS, конфигурация клиента | Выдать сертификат с правильным SAN и CA, настроить trust store |
| Приложение: `timeout` | CNI/NetworkPolicy/DNS/Service/сетевой маршрут | `get networkpolicy`, Endpoints, Pod сети | Разрешить TCP 5432 и DNS, проверить маршрут между Pod |
| `Job` бэкапа: `password authentication failed` | Пароль в БД расходится с Secret | `logs job/...`, `postgres-auth` | Синхронизировать роль PostgreSQL с Secret |
| `Job` бэкапа: `No space left on device` | Заполнен backup PVC | `describe pvc`, Job logs | Расширить PVC через CSI/процедуру; увеличить `backup.size`, почистить архивы осторожно |
| `Job` бэкапа: `DeadlineExceeded` | Дамп не уложился в час | `describe job`, лог, объём БД | Увеличить `backup.activeDeadlineSeconds`, запросы CPU/IO |
| CronJob не создаёт Job | Неправильное расписание/timeZone, `suspend`, нет доступа API | `describe cronjob`, события контроллера | Проверить cron-выражение, версию K8s, `timeZone` |
| Argo: `field is immutable` | Изменился `StatefulSet.volumeClaimTemplates` или иной immutable-параметр | Argo Sync error, diff | Вернуть совместимое поле, выполнить storage migration отдельно |
| Argo: `ComparisonError` / Helm template error | Ошибка типов, отсутствие обязательного значения, синтаксис шаблона | Argo manifest generation, `helm lint`, `helm template` | Сверить `values.yaml`, `values.schema.json`, локальный рендер |
| Новый Pod видит пустую БД | Новая/чужая идентичность PVC/PGDATA, ошибочный rename | `get pvc`, `describe pod`, логи initdb | Не проводить запись, сверить имена PV/PVC, восстановить прежний PVC или backup |

### Разбор частых случаев

**A. `Pending` + нет тома:**

```bash
kubectl -n "$NS" describe pod postgres-0
kubectl -n "$NS" describe pvc data-postgres-0
kubectl get storageclass
```

В первую очередь исправляйте StorageClass/CSI/доступность PV, а не `probes`.

**B. Сервер работает, но приложение не подключается:**

1. `kubectl -n "$NS" get svc postgres` — есть Service?
2. `kubectl -n "$NS" get endpointslice -l kubernetes.io/service-name=postgres` — есть адрес готового Pod?
3. `kubectl -n "$NS" exec postgres-0 -- psql -U postgres -d gay -c 'SELECT 1'` — PostgreSQL вообще отвечает?
4. Тест TLS по TCP из [п. 7.4](#74-проверка-tls-после-запуска).
5. Проверить **реальные** переменные приложения, ORM/драйвер и NetworkPolicy.

**C. Быстрый тест, если подозрение только на TLS:** временно `tls.enabled=false` и `tls.require=false` **только в изолированном небоевом окружении**, синхронизировать PostgreSQL и клиент, затем вернуть требование TLS. Для production это нельзя считать штатным устранением проблемы.

**D. База «пропала» после синхронизации:**

```bash
kubectl -n "$NS" get pvc -o wide
kubectl -n "$NS" describe statefulset postgres
kubectl -n "$NS" logs postgres-0 -c postgres --tail=200
```

Ищите смену PVC имени/появление новой пустой БД. **Не удаляйте `data-postgres-0` и не выполняйте `initdb` повторно**, пока не выяснили местонахождение оригинальных данных.

## 17. Рабочий чек-лист и ограничения

### Перед первым sync

- [ ] Известен реальный namespace приложения, а в Argo CD указано **то же имя**.
- [ ] Приложение ожидает именно те переменные окружения, которые указаны в его Deployment.
- [ ] Есть доступный StorageClass/PV для **двух** PVC (10Gi + 20Gi).
- [ ] Есть TLS Secret `postgres-tls` с ключами `tls.crt` и `tls.key` в целевом namespace.
- [ ] Кластер разрешает доступ к образу `postgres:16-alpine`.
- [ ] Kubernetes 1.27+ (либо `backup.timeZone: ""`).
- [ ] `helm lint` и `helm template` на той же директории chart выполнены без ошибок.
- [ ] Данные из Docker Compose либо не нужны, либо есть план `pg_dump/pg_restore`.

### После первого sync

- [ ] `postgres-0 Running 1/1 Ready`.
- [ ] PVC `data-postgres-0` и `postgres-backup` в состоянии `Bound`.
- [ ] SQL показывает базу `gay`, пользователя `postgres`, PostgreSQL 16.
- [ ] TCP TLS-проверка вернула `t`, а подключение без TLS отклоняется.
- [ ] Service `postgres` имеет готовый Endpoint.
- [ ] Приложение подключается к `postgres:5432`, нет ошибок аутентификации.
- [ ] Принудительно созданный backup Job успешно завершился.
- [ ] Проверен пробный restore в отдельную базу.

### Что здесь принципиально НЕ реализовано

- Высокая доступность (HA), реплики, Patroni/operator, автоматический failover.
- WAL-архивация и PITR (восстановление на произвольный момент времени).
- Отправка бэкапов во внешний S3/объектное хранилище; для production это крайне желательно.
- Автоматическое создание TLS-сертификата и CA.
- Миграция существующих Docker Compose-данных.
- Автоматическое изменение пароля/БД/роли в уже инициализированном PostgreSQL.
- Гарантия совместимости с конкретными CSI, PodSecurity policies, CNI и application drivers без проверки кластера.

**Итог:** для твоего исходного требования «поднять PostgreSQL 16 Alpine в одном namespace и подключить приложение» это законченный chart с TLS, PVC, пробами, ресурсами и бэкапами. Если соблюдены внешние условия, приложение должно подключаться к **`postgres:5432`**, пользователь **`postgres`**, база **`gay`**; пароль берётся из Secret `postgres-auth`.

## 18. Официальная документация

- [PostgreSQL 16 — SSL/TLS](https://www.postgresql.org/docs/16/ssl-tcp.html)
- [PostgreSQL 16 — pg_hba.conf](https://www.postgresql.org/docs/16/auth-pg-hba-conf.html)
- [PostgreSQL 16 — pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)
- [PostgreSQL 16 — pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)
- [Docker Official Image — postgres](https://hub.docker.com/_/postgres)
- [Kubernetes — StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)
- [Kubernetes — StorageClasses](https://kubernetes.io/docs/concepts/storage/storage-classes/)
- [Kubernetes — CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/)
- [Argo CD — Helm](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/)
- [Argo CD — Sync Options (Prune / Delete)](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/)

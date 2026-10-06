# Nextcloud 26 + FreeIPA + Keycloak: миграция на SSO без дублей и потери shares

> **Runbook для офлайн-homelab / Kubernetes.** Целевой компонент — Nextcloud ~26.0.2 с LDAP backend (`user_ldap`), Keycloak и **`user_oidc` 1.3.6**. В исходной конфигурации также работает Social Login. Команды ниже — шаблоны: уточнить пути, пользователя процесса PHP, namespace, pod и БД **до выполнения**.
>
> **Ограничение:** нельзя дать гарантию 100% на неизвестную инсталляцию. Этот план считается применимым **только после прохождения контрольных проверок**. Если они не проходят — не продолжать миграцию и не отключать старый вход.

## Кратко: что именно исправляем

| Объект | Пример | Для чего нужен |
|---|---|---|
| FreeIPA `uid` | `zalupa` | Логин в LDAP, **не обязательно** UID Nextcloud |
| FreeIPA `uidNumber` | `9999` | Unix UID; **не обязательно** UID Nextcloud |
| Keycloak `sub` / user ID | `99...53` | Устойчивый ID внутри Keycloak/OIDC |
| Keycloak `preferred_username` | `zalupa` | Человекочитаемый логин |
| **Nextcloud internal UID** | **`freeipa-9999`** | **Владелец файлов, адресат shares, основа привязки данных** |
| Ошибочно созданный SSO-аккаунт | `keycloak-99...53` / hash / иной ID | Отдельный backend-аккаунт, даже при таком же email |

**Критическая ошибка прошлого теста:** значение `preferred_username=zalupa` **не равно** существующему `Nextcloud UID=freeipa-9999`. При `auto_provision=true` версия `user_oidc` 1.3.6 вообще создаёт OIDC-managed user; замена `User ID mapping` на `preferred_username` не делает автоматическую привязку к LDAP-аккаунту. А **email mapping лишь назначает адрес**, не объединяет пользователей.

### Правильный принцип

```text
FreeIPA / LDAP                          Keycloak
ldap user = zalupa                      sub = 99...53
uidNumber = 9999                        preferred_username = zalupa
      │                                      │
      │ Nextcloud LDAP mapping               │ OIDC: custom claim
      ▼                                      ▼
Nextcloud existing UID = freeipa-9999   nc_uid = freeipa-9999
      ▲                                      │
      └─────────── user_oidc ────────────────┘
                 auto_provision = false

Результат: SSO открывает существующего freeipa-9999, НЕ создаёт нового.
```

**Термины:** `claim` — поле в OIDC token, `mapping` — правило «какое поле считать UID», `provisioning` — создание пользователя, `backend` — система, где Nextcloud видит пользователя, `share` — выдача прав на объект, `group` — группа доступа.

---

## 0. Стоп-условия и безопасность

До работ обязательны:

- [ ] Есть **проверенный восстанавливаемый backup** Nextcloud database + data/PVC + `config.php` + каталог apps; отдельно экспорт Keycloak realm/configuration и backup его БД.
- [ ] Проверен способ вернуть **локального администратора** без SSO; сохранены работающие настройки LDAP и Social Login.
- [ ] Есть тестовая копия с **отдельными** DB/PVC и URL, либо короткое контролируемое окно работ в рабочем экземпляре.
- [ ] Известен способ восстановить исходную конфигурацию и подтверждённая процедура восстановления PVC/БД.
- [ ] Никаких массовых удалений, ручных изменений таблиц БД, `mv` файлов в `data/`, очистки LDAP mappings.
- [ ] **Не** вводим автоматическую привязку по одному email: совпадение email не доказывает, что это одна identity.

**Важно о Kubernetes:** `kubectl cp` переносит app **только в файловую систему выбранного контейнера**, если путь не находится на подключённом persistent volume. При пересоздании pod она может исчезнуть. На ReplicaSet с несколькими pod может получиться **разный код в разных репликах при одной БД**. Поэтому `kubectl cp` допустим как временный canary в контролируемой среде; для постоянного использования нужна воспроизводимая сборка image либо явно управляемое persistent app storage. **Не масштабировать/не делать rollout до проверки пути и совместимости.** PVC не исчезает просто из-за upgrade образа, но ошибочные Helm/PVC изменения, schema-migrations или storage-политики могут повредить данные — поэтому backup обязателен.

---

## 1. Зафиксировать факты до изменений (только чтение)

**1.1. Версии, приложения, конфигурация LDAP:**

```bash
# Подставить значения для своей установки
NS=nextcloud
POD=nextcloud-xxxxxxxxx-yyyyy
CONTAINER=nextcloud

kubectl get pod -n "$NS" "$POD" -o wide
kubectl get pvc -n "$NS"
kubectl exec -n "$NS" "$POD" -c "$CONTAINER" -- sh -lc 'id; command -v php; find /opt/bitnami/nextcloud /var/www/html -maxdepth 2 -name occ 2>/dev/null'
```

Найди **реальный** каталог `occ` и запускай команды от пользователя, которому принадлежит PHP/Nextcloud (`www-data`, `daemon` либо иной — зависит от image). Пример **если** `occ=/opt/bitnami/nextcloud/occ` и текущий пользователь контейнера подходит:

```bash
# Далее внутри контейнера из директории с occ:
php occ status
php occ app:list
php occ ldap:show-config
php occ user:info freeipa-9999
php occ user:info keycloak-99...53
php occ user_oidc:provider --help
php occ config:system:get user_oidc auto_provision
```

Если `php occ` ругается на владельца процесса — выполни от **фактического** пользователя web/PHP, не пытайся править владельца всего PVC. Не публикуй client secrets/пароли из `ldap:show-config` и provider config.

**1.2. Источник истинного internal UID.** Открой `Nextcloud → Administration → LDAP/AD integration → Expert → Internal Username` и `Login Attributes`. **Не изменяй** эти поля в работающей установке: изменение не переименует задним числом старые аккаунты. Для точного соответствия пользователя Nextcloud ↔ LDAP используй:

- `php occ user:info freeipa-9999` — подтвердить, что этот UID существует и какой у него backend;
- `php occ ldap:test-user-settings '<полный LDAP DN>'` — если команда доступна, она показывает, **под каким именем LDAP user сопоставлен в Nextcloud**;
- при необходимости — **только read-only** выгрузку LDAP mapping из БД (`<db_prefix>ldap_user_mapping`, фактические имена столбцов/префикс проверить в своей БД).

**Нельзя подменять** `freeipa-9999` значением `zalupa`, `9999`, `ipaUniqueID` или `ldap_id` без проверки: в конкретной системе это могут быть совсем разные значения.

**1.3. Зафиксировать тройку на каждого тестового пользователя:**

```text
FreeIPA stable LDAP UUID / DN:  __________________
Keycloak user ID (sub):        __________________
Nextcloud EXISTING UID:        __________________    (например freeipa-9999)
```

Убедись, что Keycloak `ldap_id` действительно относится к **той же LDAP-записи**, а не просто похож на другое техническое поле.

---

## 2. Главный фикс: `user_oidc` — **только authentication**, LDAP — existing user backend

> Эти правила **проверены по исходникам тега `v1.3.6`**. Не переносить на эту версию параметры из README для новых `user_oidc`, пока не проверена их реализация именно в теге 1.3.6.

### 2.1. Отключить самостоятельное создание пользователей в `user_oidc`

Из директории с `occ` (с теми же правами, что PHP):

```bash
php occ config:system:set user_oidc auto_provision --type=boolean --value=false
php occ config:system:get user_oidc auto_provision
# Ожидается: false/0 (логическое значение false)
```

Эквивалент в `config.php`, если им управляет твой deployment:

```php
'user_oidc' => [
    'auto_provision' => false,
],
```

**Не перетри** другие ключи существующего массива `user_oidc` при редактировании. Если система генерирует `config.php` при старте, встрой настройку в **источник конфигурации** (Helm values/ConfigMap/secret template), иначе она пропадёт при recreate.

Что это даёт именно в `1.3.6`: `LoginController` берёт ID из указанного claim → вызывает LDAP search/sync → `userManager->get(ID)` → входит **в найденного существующего пользователя**; если ID не найден, **отказывает во входе**, а не создаёт нового. Это требуемое fail-closed поведение.

**Не трать время на `soft_auto_provision` / `disable_account_creation`**, описанные в новых ветках: при миграции на 1.3.6 используем подтверждённый механизм `auto_provision=false`. Сохраняй стандартный `sub` Keycloak неизменным: `nc_uid` — отдельный claim, а не подмена OIDC subject.

### 2.2. В Nextcloud User ID mapping должен содержать точный existing UID

В `Nextcloud → Administration settings → OpenID Connect → Keycloak provider`:

```text
User ID mapping = nc_uid
```

**Но только после того, как Keycloak действительно выдаёт claim `nc_uid`**. Пока `nc_uid` не существует — менять настройку бесполезно, вход не пройдёт. `preferred_username` подходит **только** если его значение уже буквально равно existing Nextcloud UID для **каждого** пользователя.

Проверка:

```text
Keycloak ID Token: { ..., "nc_uid": "freeipa-9999" }
Existing Nextcloud user:  freeipa-9999

СТРОГОЕ РАВЕНСТВО: freeipa-9999 == freeipa-9999 ✅
```

`Use unique user ID` / `providerBasedId` / SHA-256-хеширование **не решают привязку LDAP identity**. В `v1.3.6` эти правила генерации ID относятся к созданию OIDC backend users, а мы его отключаем.

---

## 3. Как получить `nc_uid` в Keycloak

### Вариант A — готовый идентификатор уже есть (лучший)

Проверь, нет ли в FreeIPA или Keycloak атрибута, который **уже равен** `freeipa-9999` для данного пользователя. Если есть, назначь его источником `nc_uid`.

В Keycloak (названия меню немного зависят от версии):

1. `User Federation → LDAP provider → Mappers` — при необходимости настроить LDAP attribute → Keycloak user attribute, например `nextcloud_uid`.
2. `Clients → Nextcloud OIDC client → Client scopes → Mappers` (либо создать отдельный client scope) → `User Attribute` mapper.
3. Заполни `User Attribute = nextcloud_uid`, `Token Claim Name = nc_uid`, `Claim JSON Type = String`, `Add to ID token = ON`. При необходимости включи `Add to UserInfo`.
4. `Clients → Nextcloud client → Client scopes → Evaluate` / тестовый ID token — проверь фактическое `nc_uid=freeipa-9999`.
5. На стороне Nextcloud выбери `User ID mapping = nc_uid`.

**Внимание:** LDAP mapper не обязан уметь составлять `freeipa-` + `uidNumber`. Keycloak `User Attribute` mapper **передаёт значение**, но не превращает `9999` в `freeipa-9999` автоматически. **Атрибут `nextcloud_uid` нельзя разрешать менять самому пользователю через Account Console или самообслуживание**: иначе пользователь может попытаться записать UID другого человека и получить его файлы. Изменение атрибута должно быть доступно только доверенному администратору/провижинингу; на тесте проверить запрет саморедактирования.

### Вариант B — `freeipa-9999` существует только как internal UID Nextcloud

Нужна **однозначная таблица соответствий**, а не подбор по email или простому username:

```csv
ldap_uuid,keycloak_id,nextcloud_uid,verified
LDAP-UUID-1,KEYCLOAK-ID-1,freeipa-9999,true
LDAP-UUID-2,KEYCLOAK-ID-2,freeipa-88888,true
```

Порядок:

1. Выгрузить **read-only** из Nextcloud LDAP user mapping соответствия `LDAP UUID → Nextcloud UID`.
2. Выгрузить из Keycloak/LDAP federation `LDAP UUID → Keycloak user ID`. Проверить, что используемый `ldap_id` — это именно тот же уникальный LDAP UUID.
3. Join по **проверенному immutable LDAP UUID**, а не по email/displayName.
4. Проверить **1:1**: у каждого Keycloak user ровно один Nextcloud UID; нет дубликатов UID, пустых значений и пропавших LDAP пользователей. Неоднозначные строки отправить на ручной разбор.
5. Для тестового пользователя добавить в Keycloak **user attribute** `nextcloud_uid=freeipa-9999`. Затем отдать его через OIDC mapper как `nc_uid`.
6. Проверить, что атрибут **не стирается** при LDAP sync и доступен через federation. Если Keycloak не сохраняет локальный атрибут federated user — не обходить это хаками: обеспечить поддерживаемое хранилище атрибута в каталоге либо отдельный штатный mapper/provisioning pipeline.
7. После успешной проверки подготовить **скрипт массового обновления через Keycloak Admin REST API** с dry-run, rate-limit, отчетом об изменениях и rollback-CSV. Работать партиями (например, 10 → 100 → остальные), не запускать сразу на миллион.
8. Репозиторий должен содержать только шаблон/алгоритм; **не коммитить** реальный CSV с users, emails, DNs, токенами, client secret или backup.

**Почему это масштабируется:** подготовка атрибутов и validation делаются централизованно; пользователи **не должны вручную заходить в Personal Settings и связывать аккаунты**. Все изменения `nextcloud_uid` должны быть аудируемыми и запрещёнными для саморедактирования пользователем: по сути этот атрибут даёт право войти в конкретный Nextcloud account.

---

## 4. Тест одного пользователя на текущем pod (canary)

**Предпочтительно** на staging-копии. Если staging нет, используй короткое окно изменений, резервный вход админа и тестовую учётку без критичных файлов.

1. **До входа:** `php occ user:info freeipa-9999`; зафиксируй backend, группы, папки и shares.
2. Проверь `config:system:get user_oidc auto_provision` → `false`.
3. Проверь **ID Token** Keycloak: `nc_uid=freeipa-9999`. Не публикуй полный token, он является чувствительным credential.
4. Проверь `User ID mapping = nc_uid` в `user_oidc`.
5. Открой Nextcloud через **новую приватную сессию браузера** и нажми **именно кнопку `user_oidc`**, не Social Login. Для исключения путаницы две кнопки временно подпиши различно.
6. **После входа:** достоверно установи фактический UID текущей сессии. Через web UI/OCS API пользователя (предпочтительно сверка `id` в ответе текущей сессии) и `php occ user:info freeipa-9999`.
7. **PASS** — вошёл именно в `freeipa-9999`, backend LDAP, все ранее доступные папки/shares на месте, новый UID не появился.
8. **FAIL** — вошёл в новый UID, в нужного пользователя не пустило, groups/files отличаются или вернулась другая кнопка: остановка, смотреть раздел 5/6; не включать rollout.

Проверка через web DevTools консоль **в уже открытой странице Nextcloud** (только чтение):

```javascript
fetch((window.OC?.webroot || '') + '/ocs/v2.php/cloud/user?format=json', {
  credentials: 'same-origin',
  headers: { 'OCS-APIRequest': 'true', 'Accept': 'application/json' }
}).then(r => r.json()).then(x => console.log('Nextcloud current UID:', x.ocs?.data?.id));
```

Если API вернул ошибку — сверяй через Network tab/официальный OCS endpoint и реальные настройки reverse proxy; ошибку API **не принимай** за доказательство другого UID.

### Проверка отрицательного сценария (обязательно)

Пробный Keycloak user с валидным токеном, но **без существующего UID Nextcloud**, должен получить **ошибку входа, без создания аккаунта**. Это доказывает, что у тебя отключен auto provisioning.

---

## 5. Если SSO всё ещё создаёт нового пользователя

Проверяй по порядку:

1. **Точно ли срабатывает `user_oidc`, а не Social Login?** Приложения имеют разные callback endpoints и разные механизмы user mapping.
2. `php occ config:system:get user_oidc auto_provision` действительно выдаёт false? Нет ли другого pod, куда направляет ingress, с иным config/app?
3. Версия реально `user_oidc=1.3.6` на **всех** репликах? Не полагайся на то, что файл скопирован в один pod.
4. В `User ID mapping` **правильно написано** поле `nc_uid` (не `preferred_name`, если claim фактически называется `preferred_username`).
5. ID Token Keycloak содержит `nc_uid` и **его фактическое значение** равно existing UID Nextcloud посимвольно (регистр, префиксы, пробелы).
6. LDAP backend `user_ldap` включен, LDAP search/sync может найти пользователя; `ldap:show-config`, `ldap:test-config`, `ldap:test-user-settings`.
7. Нет ли другого Nextcloud backend, который уже занял такой же UID?
8. Не используются ли старые cookies/SSO session браузера — тестировать с новой сессией.

**Важно:** В `1.3.6` при `auto_provision=false` если existing user не найден, **обычный результат — отказ во входе**. Появление нового пользователя в этом случае — сигнал, что реально работает другая ветка/приложение/конфигурация. Не лечить это изменением email.

---

## 6. Если internal UID один, но через разные способы входа разные папки

Это **отдельный дефект**, он не исправляется `User ID mapping`, если `id` обеих сессий уже совпадает.

### 6.1. Убедиться, что UID в двух СЕССИЯХ одинаковый

Не ориентироваться лишь на то, сколько строк в `Users` у администратора. В каждой из двух сессий зафиксировать actual `id` через текущий OCS user endpoint (см. раздел 4).

### 6.2. Проверить изменение групп

Записать `php occ user:info <UID>` **до и после каждого логина**. Проверить FreeIPA memberships и Keycloak `groups` claim. Отдельно выяснить, не меняет ли Social Login группы при входе.

- **Если login идёт через Social Login:** опция `Do not prune not available user groups on login` может предотвратить удаление отсутствующих в токене групп. Это действует **только на Social Login** и не заменяет правильную LDAP/Keycloak group mapping.
- **Если login идёт через `user_oidc` + `auto_provision=false`:** управление существующим LDAP user остаётся у LDAP backend. Настройка `Do not prune...` Social Login здесь **не применяется**.
- Не выдавать доступ «всем» ради сокрытия ошибки: права только целевым FreeIPA/Nextcloud группам по принципу least privilege.

### 6.3. Если группы не меняются

Разделить «пропавшую папку» по происхождению:

| Папка | Где проверять |
|---|---|
| Собственная | UID/файловое хранилище, фактическая сессия, client cache |
| Shared to user | Адресат incoming share — **какой internal UID**? |
| Shared to group / Group folders | Nextcloud membership, Groupfolders ACL, ограничения приложения |
| External Storage (SMB/NFS) | Mount `Available for`, разрешения на SMB/LDAP, per-user credentials, доступность сервера |
| Access Control / workflow | Условия правил, группы, IP/контекст, логи отказов |

Повторить тест в чистой сессии; сопоставить Nextcloud logs с временем входа. **Одна строка пользователя в админке сама по себе не означает, что login route и права идентичны**.

---

## 7. Что делать с уже созданными дублями

**Не удалять автоматически** `keycloak-*` или `user_oidc-*` аккаунты! Туда могли попасть новые файлы, shares, входящие shares, ссылки, комментарии и другие metadata.

Порядок на каждого дубля:

1. Составить список **old LDAP UID ↔ new SSO UID** и проверить соответствие по immutable ID.
2. Выбрать **целевой аккаунт** (обычно LDAP `freeipa-*`), на котором уже живёт исторический контент.
3. Исправить **новые** входы: `user_oidc auto_provision=false`, `nc_uid=old UID`. Проверить SSO → старый аккаунт.
4. Провести инвентаризацию дубля: собственные файлы, исходящие/входящие shares, groupfolders, шары по ссылке, настроенные клиенты, encryption.
5. Если у дубля **есть собственные файлы**, изучить доступную в **NC26** команду `php occ files:transfer-ownership --help` и провести перенос в тесте/с backup. Синтаксис основного варианта (запускается **только после проверки**):

   ```bash
   php occ files:transfer-ownership SOURCE_SSO_UID DESTINATION_LDAP_UID
   ```

   Incoming shares могут потребовать отдельной обработки (`--transfer-incoming-shares`, если опция есть в вашей версии) и проверки со стороны владельца. Не считать команду универсальным «merge всей учетной записи».
6. Если файлов нет, всё равно проверить shares/активность/данные приложений; после согласованной миграции — **штатно** деактивировать или удалить только подтверждённый дубль.
7. **Без массовой команды удаления пользователей**: сначала canary, затем маленькая партия с журналом результатов.

**Не выполнять:** `DELETE/UPDATE` напрямую в Nextcloud DB, `mv`/`rsync` между `data/<UID>` без штатного переноса, «Clear mappings» LDAP.

---

## 8. Массовое внедрение для 100 / 10 000 / 1 000 000 пользователей

**Без ручного входа каждого пользователя:**

```text
1. READ-ONLY: LDAP UUID -> existing Nextcloud UID
2. READ-ONLY: LDAP UUID -> Keycloak user ID
3. VALIDATE 1:1, collision / missing / duplicate -> FAIL
4. PUBLISH: verified nc_uid to Keycloak users (Admin REST / supported federation)
5. OIDC mapper: user attribute -> ID token nc_uid
6. Nextcloud user_oidc: auto_provision=false; User ID mapping=nc_uid
7. CANARY: 1 user -> 10 -> 100 -> all
8. AUDIT: same UID, no new users, same shares, same groups
9. HIDE direct login + disable Social Login only after acceptance
10. DELETE duplicates in separate, controlled migration
```

На масштабе обязательны **idempotency**, checkpoint/resume, записи `before/after`, ограничение нагрузки на LDAP/Keycloak/Nextcloud, возврат только изменённых настроек, и ручной разбор **всех** неоднозначных user mappings. Если какое-либо соответствие не доказано, **не авторизовать** этого пользователя в чужой Nextcloud UID.

---

## 9. Offline-Kubernetes: как закрепить результат после успешного теста

1. На временном pod можно проверить **логику** `auto_provision=false` и `nc_uid`, но учесть, что DB-конфигурация может быть общей для всех pod.
2. Проверить `php occ status`, app version, путь установки, совместимость `user_oidc 1.3.6` с фактической NC26, права файлов и возможность активации app.
3. Для постоянной эксплуатации добавить **проверенный архив** `user_oidc` 1.3.6 и checksum в CI/build-context (при наличии разрешённого источника) → собрать **immutable custom image** во внешней среде → загрузить в **локальный registry** homelab.
4. Не изменять PVC templates/volume names/storageClass при upgrade приложения: Helm diff должен показывать **только image/app/config**, а не изменение хранения.
5. В rollout держать один согласованный набор app-кода на всех репликах. Избегать mixed-version Nextcloud pods на общей БД, если совместимость не подтверждена.
6. Перенести `auto_provision=false` в устойчивую configuration-as-code, учесть secrets через K8s Secret/Vault; повторно проверить его после recreate pod.
7. После canary сделать restore drill / rollback rehearsal: можно ли вернуться к предыдущему image и config, не нарушив состояние БД? **Если app schema migration делает откат несовместимым — нужен restore БД из backup**, а не только `kubectl rollout undo`.
8. Лишь после успешной миграции выключать Social Login/прямой LDAP login. **Сам backend LDAP/FreeIPA не выключать**: users `freeipa-*` по-прежнему находятся в нём.

**Offline не мешает SSO**, если браузер, Nextcloud pod и Keycloak имеют доступ к нужным **локальным** DNS/FQDN, TLS-сертификатам и OIDC discovery/token/JWKS endpoints. Для пользователя внешняя сеть/Интернет не требуется.

---

## 10. Acceptance criteria и rollback

### PASS — можно переходить к следующей партии

- [ ] `Keycloak claim nc_uid == существующий Nextcloud UID` по проверенному LDAP UUID.
- [ ] `user_oidc auto_provision=false` реально действует в текущем запущенном приложении.
- [ ] После SSO входа actual session UID **равен старому** `freeipa-*` UID.
- [ ] Число пользователей Nextcloud не выросло из-за тестового login.
- [ ] Собственные файлы, user shares, group shares, Group folders и external mounts проходят проверку.
- [ ] Новый SSO user без сопоставления получает отказ, а не новый аккаунт.
- [ ] Группы и права до/после не ухудшились.
- [ ] Прямой локальный admin login и аварийный доступ сохранены.
- [ ] После recreate pod код, конфигурация и SSO остаются работоспособны.

### FAIL — немедленно остановить внедрение

- Claim отсутствует / неоднозначен, Nextcloud UID не совпадает, создан новый UID, нельзя восстановить rollback, ломаются shares/ACL или LDAP backend не находит пользователя.

### Rollback

1. **Не трогать данные и не удалять дубли**.
2. Вернуть старые app login paths (Social Login / direct LDAP / local admin).
3. Отключить `user_oidc` или вернуть прежний `mapping`/provider config, если это необходимо для восстановления входа; вернуть исходный конфиг из сохранённого snapshot.
4. При необходимости вернуть **предыдущий app image**. Если изменения затронули **БД/schema** — восстанавливать **согласованную** копию DB + data/config; один rollback image не гарантия.
5. Повторить `occ status`, `user:info`, вход/права/files для тестовых пользователей. Зафиксировать причину FAIL до новых попыток.

---

## 11. Почему прошлые попытки не помогли — итог

| Тест | Почему не дал нужного результата |
|---|---|
| `User ID mapping=preferred_username` | `zalupa` ≠ `freeipa-9999`; плюс оставалось `auto_provision=true` |
| `User ID mapping=email` | Email — профильный атрибут, а не проверенная связка с existing Nextcloud UID |
| Social Login `already connected` | Внешняя identity уже занята/привязана — это не массовый мигратор |
| Создать всем новых `keycloak-*` и раздать папки | Возможно для ограниченного набора group-shares, но **не сохраняет личное владение и существующие incoming shares**, усложняет аудит |
| Social Login `Do not prune...` | Способен помочь только с **удалением групп Social Login**, но не с mismatch UID и не влияет на `user_oidc` |
| Просто выключить FreeIPA | Ломает зависимость существующих `freeipa-*` аккаунтов от LDAP backend |

### Единственное рекомендуемое целевое поведение

```text
Keycloak (кто вошёл, token с nc_uid)
   ↓
user_oidc 1.3.6 (auto_provision=false)
   ↓
Nextcloud user_ldap (уже существующий freeipa-* UID + группы)
   ↓
ТЕ ЖЕ файлы, shares и права.
```

**Без новых Nextcloud пользователей. Без ручного linking в каждом профиле. Без удаления FreeIPA.** Но результат возможен только при доказанном строгом соответствии `Keycloak identity → existing Nextcloud UID` и успешных контрольных тестах.

---

## Проверенные первоисточники (лучше сохранять ссылки на конкретный тег)

1. `user_oidc` **v1.3.6 README** (auto provisioning off, exact LDAP ID requirement): https://raw.githubusercontent.com/nextcloud/user_oidc/v1.3.6/README.md
2. `user_oidc` **v1.3.6 LoginController.php** (ветка `autoProvisionAllowed=false`, получение existing user): https://raw.githubusercontent.com/nextcloud/user_oidc/v1.3.6/lib/Controller/LoginController.php
3. `user_oidc` **v1.3.6 LocalIdService.php** (hash/provider-based IDs): https://raw.githubusercontent.com/nextcloud/user_oidc/v1.3.6/lib/Service/LocalIdService.php
4. `user_oidc` **v1.3.6 ProvisioningService.php** (создание OIDC-backed users при auto-provision): https://raw.githubusercontent.com/nextcloud/user_oidc/v1.3.6/lib/Service/ProvisioningService.php
5. Nextcloud **26** LDAP user/group mapping: https://docs.nextcloud.com/server/26/admin_manual/configuration_user/user_auth_ldap.html
6. Nextcloud **26** OCS API: https://docs.nextcloud.com/server/26/developer_manual/client_apis/OCS/ocs-api-overview.html
7. Nextcloud `occ` (проверить поддержку команд в `php occ ... --help` своей версии): https://docs.nextcloud.com/server/latest/admin_manual/occ_ldap.html
8. Keycloak Protocol Mappers: https://www.keycloak.org/admin-api/protocol-mappers
9. Social Login group settings: https://github.com/zorn-v/nextcloud-social-login
10. Nextcloud `files:transfer-ownership`: https://docs.nextcloud.com/server/latest/admin_manual/occ_files.html

> **Безопасность репозитория:** только runbook и примеры. Не размещать в GitHub дампы БД, реальные user exports, LDAP DN/email, private token, access token, Keycloak client secret, пароль LDAP bind-account или Keycloak realm backup с секретами.

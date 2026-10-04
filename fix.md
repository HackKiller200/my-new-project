# Keycloak + FreeIPA + SSO: диагностика и исправление дублей пользователей

## Цель

Добиться такой схемы:

```text
FreeIPA / LDAP
      |
      | users / groups / credentials
      v
   Keycloak
      |
      | OIDC / SSO
      v
   Service
      |
      v
ОДИН existing user
      |
      +-- files
      +-- shares
      +-- permissions
```

Итог: пользователь входит только через SSO/Keycloak, но получает **свой старый аккаунт в сервисе** со всеми файлами, правами и шарами.

---

# 1. В чем проблема

Один реальный человек может существовать в сервисе как **две разные identity**.

Например:

```text
FreeIPA:
uid = jack

Keycloak:
preferred_username = jack
sub = 8f13a7d2-...
```

Для человека это один Jack.

Но сервис может видеть:

```text
LDAP login  -> user = jack
SSO login   -> user = 8f13a7d2-...
```

Тогда внутри сервиса появляются два разных пользователя:

```text
User A:
ID = jack

User B:
ID = 8f13a7d2-...
```

Именно поэтому через FreeIPA и через SSO могут отображаться разные папки и разные shares.

---

# 2. Почему папки могут отличаться

Файлы и права обычно привязаны не к "человеку", а к внутреннему пользователю сервиса.

Пример:

```text
Folder-A -> user jack
Folder-B -> user jack
Common   -> group devops
```

Если:

```text
FreeIPA login -> user jack
SSO login     -> user 8f13a7d2
```

то получится:

```text
FreeIPA:
- Folder-A
- Folder-B
- Common

SSO:
- Common
```

`Common` виден обоим, потому что доступ выдан группе.

`Folder-A` и `Folder-B` видны только старому пользователю `jack`.

---

# 3. Что такое `sub`

`sub` = Subject Identifier.

Это технический уникальный ID пользователя, который Keycloak передает сервису через OIDC.

Пример:

```json
{
  "sub": "8f13a7d2-1234-...",
  "preferred_username": "jack",
  "email": "jack@example.local"
}
```

Где:

```text
preferred_username = обычный логин пользователя
sub                = технический уникальный ID пользователя
```

Сам по себе `sub` не является проблемой.

Проблема появляется, если сервис использует:

```text
User ID = sub
```

а старый LDAP-пользователь в сервисе имеет:

```text
User ID = jack
```

Тогда:

```text
jack != 8f13a7d2-...
```

и сервис считает их разными пользователями.

---

# 4. Что такое mapping

Mapping — это правило:

> Какое поле из Keycloak считать идентификатором пользователя в сервисе.

Пример неправильного для существующего LDAP-пользователя варианта:

```text
OIDC User ID mapping = sub
```

Получается:

```text
Keycloak:
sub = 8f13a7d2-...

Service:
user = 8f13a7d2-...
```

Но старый пользователь:

```text
LDAP:
uid = jack

Service:
user = jack
```

В результате два пользователя.

Нам нужно добиться:

```text
LDAP:
uid = jack

Keycloak:
preferred_username = jack
или
uid = jack

Service:
internal user = jack
```

---

# 5. Что НЕ надо делать сразу

До завершения диагностики:

- не удалять LDAP/FreeIPA;
- не удалять дубли пользователей;
- не переносить папки вручную через файловую систему;
- не менять пользователей напрямую в БД;
- не менять `sub` без необходимости;
- не включать массовое автоматическое создание новых SSO-пользователей;
- не отключать старый способ входа до проверки нескольких пользователей.

---

# 6. Алгоритм диагностики

Берем ОДНОГО пользователя, у которого через FreeIPA и через SSO разные папки.

Например:

```text
jack
```

## 6.1 Проверить FreeIPA

Нужно узнать:

```text
uid
mail
ipaUniqueID
groups
```

Пример:

```text
uid = jack
mail = jack@example.local
ipaUniqueID = abc123...
groups = devops, cloud-users, admins
```

Главное значение:

```text
uid = jack
```

---

## 6.2 Проверить Keycloak

Открыть:

```text
Users -> jack
```

Проверить:

```text
Username
Email
ID
Federation Link
Attributes
Groups
```

Пример:

```text
Username = jack
Email = jack@example.local
ID = 8f13a7d2-...
Federation Link = FreeIPA LDAP provider
```

---

## 6.3 Проверить OIDC claims

Нужно понять, что Keycloak реально отправляет сервису.

Типичный набор:

```json
{
  "sub": "8f13a7d2-...",
  "preferred_username": "jack",
  "email": "jack@example.local"
}
```

Проверить минимум:

```text
sub
preferred_username
email
groups
```

---

## 6.4 Проверить пользователя в сервисе

Нужно найти:

```text
internal user ID при LDAP-входе
internal user ID при SSO-входе
```

Пример проблемы:

```text
LDAP login:
internal ID = jack

SSO login:
internal ID = 8f13a7d2-...
```

Это означает:

```text
ПРОБЛЕМА = User Mapping
```

---

# 7. Вариант 1: разные Internal User ID

Пример:

```text
LDAP -> jack
SSO  -> 8f13a7d2
```

## Исправление

Нужно заставить SSO использовать тот же идентификатор, что и старый LDAP user.

Предпочтительная логика:

```text
FreeIPA uid
      |
      v
Keycloak username / custom uid claim
      |
      v
Service User ID
```

То есть:

```text
uid = jack
preferred_username = jack
Service user = jack
```

---

# 8. Если хватает `preferred_username`

Если:

```text
FreeIPA uid = jack
Keycloak preferred_username = jack
```

то в сервисе можно настроить:

```text
OIDC User ID mapping = preferred_username
```

Тогда:

```text
LDAP login:
jack

SSO login:
jack
```

Оба способа входа должны привести к одному internal user.

---

# 9. Если нужен отдельный `uid` claim

Если `preferred_username` использовать нельзя, можно передать LDAP `uid` отдельным claim.

## Шаг 1. LDAP mapper в Keycloak

Путь примерно такой:

```text
User Federation
-> LDAP provider
-> Mappers
```

Создать mapper:

```text
LDAP Attribute:
uid

Keycloak User Attribute:
ldap_uid
```

Получается:

```text
FreeIPA:
uid = jack

      |
      v

Keycloak:
ldap_uid = jack
```

---

## Шаг 2. Protocol Mapper

В client сервиса создать mapper:

```text
Mapper type:
User Attribute

User Attribute:
ldap_uid

Token Claim Name:
uid

Claim JSON Type:
String

Add to ID token:
ON

Add to access token:
ON

Add to UserInfo:
ON
```

После этого Keycloak должен отдавать:

```json
{
  "sub": "8f13a7d2-...",
  "preferred_username": "jack",
  "uid": "jack"
}
```

---

## Шаг 3. Mapping в сервисе

В сервисе:

```text
OIDC User ID mapping = uid
```

Тогда:

```text
OIDC claim:
uid = jack

      |
      v

Service:
existing user = jack
```

---

# 10. Вариант 2: Internal User ID одинаковый, но папки разные

Пример:

```text
LDAP login -> jack
SSO login  -> jack
```

То есть пользователь действительно один.

Но папки все равно отличаются.

Тогда проверяем группы.

Пример:

```text
FreeIPA groups:
- devops
- admins
- cloud-users

Keycloak groups:
- devops
- cloud-users
```

Отсутствует:

```text
admins
```

А доступ выдан так:

```text
AdminFolder -> group admins
```

Поэтому через SSO папка не видна.

---

# 11. Исправление групп

## Шаг 1. Проверить LDAP Group Mapper

В Keycloak:

```text
User Federation
-> LDAP provider
-> Mappers
```

Проверить или создать:

```text
Group Mapper
```

Он должен преобразовывать:

```text
FreeIPA LDAP groups
        |
        v
Keycloak groups
```

Например:

```text
FreeIPA:
devops
admins
cloud-users

      |
      v

Keycloak:
/devops
/admins
/cloud-users
```

---

## Шаг 2. Передать группы в OIDC token

Для client сервиса создать:

```text
Group Membership Mapper
```

Настройки примерно:

```text
Token Claim Name:
groups

Add to ID token:
ON

Add to access token:
ON

Add to UserInfo:
ON
```

В token должно появиться:

```json
{
  "groups": [
    "devops",
    "admins",
    "cloud-users"
  ]
}
```

---

## Шаг 3. Проверить Group Mapping в сервисе

Сервис должен использовать:

```text
Group claim = groups
```

После этого проверить доступ к group-based shares.

---

# 12. JIT / Auto Provisioning

JIT provisioning = автоматическое создание пользователя при первом SSO-входе.

Логика:

```text
SSO login
    |
    v
user найден?
    |
   НЕТ
    |
    v
CREATE NEW USER
```

Это удобно для новой системы.

Но при миграции существующих LDAP users может создавать дубли.

Поэтому на этапе миграции желательно:

```text
не создавать нового user,
если должен использоваться уже существующий LDAP user
```

Если сервис умеет:

```text
Auto create users = OFF
```

или:

```text
Disable account creation = ON
```

на время миграции это безопаснее.

---

# 13. Что делать с уже созданными дублями

Пример:

```text
Old LDAP user:
jack

New OIDC user:
8f13a7d2
```

## Сначала

Исправить mapping так, чтобы новые дубли больше не создавались.

Проверить:

```text
SSO login -> existing user jack
```

Только потом работать с дублем.

---

## Если у дубля нет данных

Проверить:

```text
files = 0
shares = 0
important permissions = 0
```

После проверки дубль можно удалить/отключить штатными средствами сервиса.

---

## Если у дубля уже есть данные

Использовать только штатный:

```text
Transfer Ownership
Move Data
Merge Account
Account Linking
```

или аналогичный механизм сервиса.

Не делать:

```text
mv /data/user1 /data/user2
```

Не делать ручные:

```sql
UPDATE users ...
```

Причина: сервис может хранить отдельно:

```text
file metadata
share IDs
ACL
file cache
database references
ownership
```

---

# 14. Рекомендуемая схема миграции

Самый безопасный вариант:

```text
FreeIPA
   |
   | user store
   v
Keycloak
   |
   | authentication / SSO
   v
Service
   |
   | existing internal account
   v
jack
```

То есть:

```text
FreeIPA = источник пользователей
Keycloak = единая точка входа
Service = файлы, shares, ACL
```

Пользователь больше не должен напрямую выбирать:

```text
Login with LDAP
или
Login with Keycloak
```

Внешне остается:

```text
Login with Keycloak
```

Но Keycloak внутри может продолжать работать с FreeIPA через LDAP User Federation.

---

# 15. Порядок внедрения

Делать строго по шагам:

1. Выбрать одного проблемного пользователя.
2. Узнать его `uid` в FreeIPA.
3. Узнать `preferred_username`, `sub`, email и groups в Keycloak.
4. Узнать Internal User ID в сервисе при LDAP-входе.
5. Узнать Internal User ID при SSO-входе.
6. Если ID разные — чинить User Mapping.
7. Если ID одинаковые — сравнить groups.
8. Если groups разные — чинить Group Mapping.
9. Запретить автоматическое создание дублей на время миграции.
10. Проверить SSO login.
11. Проверить старые файлы.
12. Проверить старые shares.
13. Проверить group permissions.
14. Проверить еще 3-5 пользователей.
15. Только после этого исправлять старые дубли.
16. После успешного теста отключить прямой LDAP login.
17. Оставить Keycloak единой точкой входа.
18. FreeIPA пока оставить как user directory за Keycloak.

---

# 16. Проверочный чек-лист

Для каждого тестового пользователя:

```text
[ ] FreeIPA uid известен
[ ] Keycloak username известен
[ ] Keycloak sub известен
[ ] preferred_username проверен
[ ] email совпадает
[ ] groups проверены
[ ] LDAP Internal User ID известен
[ ] SSO Internal User ID известен
[ ] LDAP ID = SSO ID
[ ] старые files доступны
[ ] direct shares доступны
[ ] group shares доступны
[ ] новый дубль не создается
```

---

# 17. Как быстро определить тип проблемы

```text
                  SSO login
                     |
                     v
       Internal User ID совпадает?
              /             \
            НЕТ              ДА
            |                |
            v                v
      USER MAPPING       Groups совпадают?
                             /        \
                           НЕТ         ДА
                           |           |
                           v           v
                     GROUP MAPPING   Shares / ACL /
                                     cache / service
                                     specific logic
```

---

# 18. Rollback

Перед изменениями:

```text
- сохранить текущую конфигурацию Keycloak client
- сохранить настройки LDAP/User Federation
- сохранить настройки OIDC integration в сервисе
- зафиксировать текущие User ID тестового пользователя
- не удалять старые аккаунты
```

Если после изменения SSO перестал открывать правильного пользователя:

```text
1. вернуть прежний User ID mapping;
2. вернуть предыдущий mapper;
3. включить старый способ входа;
4. проверить test user;
5. только потом продолжать диагностику.
```

---

# 19. Ключевая мысль

Проблема не в том, что:

```text
"файлы плохо синхронизируются"
```

Проблема обычно в том, что:

```text
FreeIPA login -> identity A
SSO login     -> identity B
```

или:

```text
identity одна,
но группы/права приходят разные.
```

Чинить нужно:

```text
User Mapping
```

и при необходимости:

```text
Group Mapping
```

Целевая схема:

```text
FreeIPA user
    |
    v
Keycloak user
    |
    v
ТОТ ЖЕ existing user в сервисе
    |
    +-- старые files
    +-- старые shares
    +-- старые permissions
```

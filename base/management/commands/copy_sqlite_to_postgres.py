"""
Copy committed SQLite rows into the default database via the Django ORM.

SQLite stores UUIDField as char(32); Postgres uses native uuid. Going through
the ORM (not raw SQL) is what makes that conversion, and JSONField round-trips,
safe. Primary keys are preserved so FKs keep pointing at the same rows.

Usage (after DATABASE_URL points at Postgres):

    python manage.py copy_sqlite_to_postgres
    python manage.py copy_sqlite_to_postgres --force
    python manage.py copy_sqlite_to_postgres --sqlite /path/to/db.sqlite3
"""

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, transaction

from laundry.models import (
    AdminAuditLog,
    Administrator,
    AvailabilityMiss,
    Booking,
    Exchange,
    Hostel,
    Institute,
    InstituteRule,
    Machine,
    Notification,
    NotificationPreference,
    Strike,
    Student,
    SuperAdministrator,
    Ticket,
    TicketEvent,
)
from mcp_server.models import McpToken
from mcp_server.oauth_models import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthRefreshToken,
)

SOURCE_ALIAS = "source"

# Ephemeral / auto-rebuilt. Never copied.
SKIPPED_TABLES = ("django_session", "django_admin_log", "lundrii_cache")

# Parents first. Permission / ContentType rows are created by migrate; M2M
# through tables remap permission ids by natural key.
LAUNDRY_COPY_ORDER = (
    Institute,
    InstituteRule,
    Hostel,
    Student,
    Administrator,
    SuperAdministrator,
    Machine,
    Booking,
    Exchange,
    AvailabilityMiss,
    Ticket,
    TicketEvent,
    Strike,
    Notification,
    NotificationPreference,
    AdminAuditLog,
)

# OAuthRefreshToken is copied in a two-pass helper (self-FK rotated_from).
MCP_COPY_ORDER_BEFORE_REFRESH = (OAuthClient, OAuthAuthorizationCode)
MCP_COPY_ORDER_AFTER_REFRESH = (McpToken,)


def sqlite_file_config(path: Path) -> dict:
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(path),
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "OPTIONS": {
            "timeout": 20,
            "init_command": "PRAGMA foreign_keys=ON;",
        },
        "TIME_ZONE": None,
        "USER": "",
        "PASSWORD": "",
        "HOST": "",
        "PORT": "",
        "TEST": {
            "CHARSET": None,
            "COLLATION": None,
            "MIGRATE": True,
            "MIRROR": None,
            "NAME": None,
        },
    }


def register_sqlite_alias(alias: str, path: Path | str) -> None:
    """Point a Django DB alias at a SQLite file, replacing a stale wrapper."""
    path = Path(path).resolve()
    config = sqlite_file_config(path)
    current = connections.settings.get(alias)
    if current is not None:
        try:
            same_file = Path(str(current.get("NAME") or "")).resolve() == path
        except OSError:
            same_file = False
        if same_file and current.get("ENGINE") == config["ENGINE"]:
            return
    settings.DATABASES[alias] = config
    connections.settings[alias] = config
    try:
        del connections[alias]
    except Exception:
        pass


def unregister_sqlite_alias(alias: str) -> None:
    try:
        del connections[alias]
    except Exception:
        pass
    connections.settings.pop(alias, None)
    if alias != DEFAULT_DB_ALIAS:
        settings.DATABASES.pop(alias, None)


def _is_sqlite(alias: str) -> bool:
    return connections[alias].vendor == "sqlite"


def _sqlite_file(alias: str) -> Path | None:
    if not _is_sqlite(alias):
        return None
    name = connections[alias].settings_dict.get("NAME")
    if not name or name == ":memory:":
        return None
    return Path(name).resolve()


def _table_exists(alias: str, table: str) -> bool:
    return table in connections[alias].introspection.table_names()


def _clone(model, obj, *, null_attnames=()):
    kwargs = {}
    for field in model._meta.concrete_fields:
        if getattr(field, "generated", False):
            continue
        if field.attname in null_attnames:
            kwargs[field.attname] = None
        else:
            kwargs[field.attname] = getattr(obj, field.attname)
    return model(**kwargs)


def _copy_model(model, source: str, dest: str, *, null_attnames=()) -> int:
    clones = [
        _clone(model, obj, null_attnames=null_attnames)
        for obj in model.objects.using(source).order_by("pk").iterator()
    ]
    if clones:
        model.objects.using(dest).bulk_create(clones)
    return len(clones)


def _permission_pk_map(source: str, dest: str) -> dict:
    """Map source Permission.pk → dest Permission.pk by natural key."""

    def key(perm):
        return (perm.content_type.app_label, perm.content_type.model, perm.codename)

    src = {
        key(p): p.pk
        for p in Permission.objects.using(source).select_related("content_type")
    }
    dst = {
        key(p): p.pk
        for p in Permission.objects.using(dest).select_related("content_type")
    }
    return {src_pk: dst[k] for k, src_pk in src.items() if k in dst}


def _copy_through(through, source: str, dest: str, *, remap=None) -> int:
    """Copy an M2M through table. ``remap`` is {attname: {old_id: new_id}}."""
    remap = remap or {}
    clones = []
    for obj in through.objects.using(source).iterator():
        kwargs = {}
        skip = False
        for field in through._meta.concrete_fields:
            if field.primary_key or getattr(field, "generated", False):
                continue
            value = getattr(obj, field.attname)
            if field.attname in remap:
                value = remap[field.attname].get(value)
                if value is None:
                    skip = True
                    break
            kwargs[field.attname] = value
        if not skip:
            clones.append(through(**kwargs))
    if clones:
        through.objects.using(dest).bulk_create(clones)
    return len(clones)


def _copy_refresh_tokens(source: str, dest: str) -> int:
    """Insert OAuthRefreshToken rows, then restore rotated_from (self-FK)."""
    model = OAuthRefreshToken
    clones = []
    delayed = []
    for obj in model.objects.using(source).order_by("pk").iterator():
        clone = _clone(model, obj)
        rotated = clone.rotated_from_id
        clone.rotated_from_id = None
        clones.append(clone)
        if rotated is not None:
            delayed.append((clone.pk, rotated))
    if clones:
        model.objects.using(dest).bulk_create(clones)
    for pk, rotated in delayed:
        model.objects.using(dest).filter(pk=pk).update(rotated_from_id=rotated)
    return len(clones)


def _copy_jwt_blacklist(source: str, dest: str) -> dict[str, int]:
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken,
            OutstandingToken,
        )
    except ImportError:
        return {}
    counts = {}
    if not _table_exists(source, OutstandingToken._meta.db_table):
        return counts
    if not _table_exists(dest, OutstandingToken._meta.db_table):
        return counts
    counts[OutstandingToken._meta.label] = _copy_model(
        OutstandingToken, source, dest
    )
    if _table_exists(source, BlacklistedToken._meta.db_table) and _table_exists(
        dest, BlacklistedToken._meta.db_table
    ):
        counts[BlacklistedToken._meta.label] = _copy_model(
            BlacklistedToken, source, dest
        )
    return counts


def _reset_sequences(models, dest: str) -> None:
    conn = connections[dest]
    sqls = conn.ops.sequence_reset_sql(no_style(), models)
    if not sqls:
        return
    with conn.cursor() as cursor:
        for sql in sqls:
            cursor.execute(sql)


def copy_rows(*, source: str = SOURCE_ALIAS, dest: str = DEFAULT_DB_ALIAS) -> dict[str, int]:
    """
    Copy domain rows from ``source`` into ``dest`` in FK order.

    Skips sessions, admin.LogEntry, and lundrii_cache. Permission rows are not
    copied (migrate already created them); M2M links are remapped.
    """
    User = get_user_model()
    counts: dict[str, int] = {}
    sequence_models = [Group, User.groups.through, User.user_permissions.through]
    sequence_models.append(Group.permissions.through)

    with transaction.atomic(using=dest):
        counts[Group._meta.label] = _copy_model(Group, source, dest)
        counts[User._meta.label] = _copy_model(User, source, dest)

        perm_map = _permission_pk_map(source, dest)
        counts[f"{Group._meta.label}.permissions"] = _copy_through(
            Group.permissions.through,
            source,
            dest,
            remap={"permission_id": perm_map},
        )
        counts[f"{User._meta.label}.groups"] = _copy_through(
            User.groups.through, source, dest
        )
        counts[f"{User._meta.label}.user_permissions"] = _copy_through(
            User.user_permissions.through,
            source,
            dest,
            remap={"permission_id": perm_map},
        )

        for model in LAUNDRY_COPY_ORDER:
            counts[model._meta.label] = _copy_model(model, source, dest)

        for model in MCP_COPY_ORDER_BEFORE_REFRESH:
            counts[model._meta.label] = _copy_model(model, source, dest)
        counts[OAuthRefreshToken._meta.label] = _copy_refresh_tokens(source, dest)
        for model in MCP_COPY_ORDER_AFTER_REFRESH:
            counts[model._meta.label] = _copy_model(model, source, dest)

        jwt_counts = _copy_jwt_blacklist(source, dest)
        counts.update(jwt_counts)
        if jwt_counts:
            from rest_framework_simplejwt.token_blacklist.models import (
                BlacklistedToken,
                OutstandingToken,
            )

            sequence_models.extend([OutstandingToken, BlacklistedToken])

        _reset_sequences(sequence_models, dest)

    return counts


class Command(BaseCommand):
    help = (
        "Copy rows from a SQLite file into the default database via the ORM. "
        "Preserves UUID primary keys. Refuses to run when default is still "
        "SQLite, or when the target already has users (unless --force)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sqlite",
            default=str(settings.BASE_DIR / "db.sqlite3"),
            help="Path to the source SQLite file (default: backend/db.sqlite3).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Copy even if the target database already has users.",
        )
        parser.add_argument(
            "--allow-sqlite",
            action="store_true",
            help=(
                "Allow copying when default is SQLite. For offline tests that "
                "use two SQLite files; production must target Postgres."
            ),
        )

    def handle(self, *args, **options):
        verbosity = options.get("verbosity", 1)
        allow_sqlite = options["allow_sqlite"]
        force = options["force"]
        source_path = Path(options["sqlite"]).expanduser()
        if not source_path.is_absolute():
            source_path = (Path.cwd() / source_path).resolve()
        else:
            source_path = source_path.resolve()

        if _is_sqlite(DEFAULT_DB_ALIAS) and not allow_sqlite:
            raise CommandError(
                "Refusing to copy: default database is still SQLite. "
                "Set DATABASE_URL to a postgres:// URI and retry."
            )

        User = get_user_model()
        if User.objects.using(DEFAULT_DB_ALIAS).exists() and not force:
            raise CommandError(
                "Refusing to copy: the target database already has users. "
                "Pass --force if you intend to copy anyway."
            )

        if not source_path.is_file():
            raise CommandError(f"SQLite file not found: {source_path}")

        dest_file = _sqlite_file(DEFAULT_DB_ALIAS)
        if dest_file is not None and dest_file == source_path:
            raise CommandError(
                "Refusing to copy a SQLite file onto itself. "
                "Point default at Postgres (or a different SQLite file)."
            )

        register_sqlite_alias(SOURCE_ALIAS, source_path)

        if verbosity >= 1:
            self.stdout.write("Migrating default database…")
        call_command(
            "migrate",
            database=DEFAULT_DB_ALIAS,
            interactive=False,
            verbosity=verbosity,
        )

        counts = copy_rows(source=SOURCE_ALIAS, dest=DEFAULT_DB_ALIAS)
        total = sum(counts.values())
        if verbosity >= 2:
            for label, n in counts.items():
                self.stdout.write(f"  {label}: {n}")
        if verbosity >= 1:
            self.stdout.write(
                self.style.SUCCESS(f"Copied {total} rows from {source_path}.")
            )
            self.stdout.write(
                "Skipped sessions, admin.LogEntry, and lundrii_cache."
            )

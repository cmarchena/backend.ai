from __future__ import annotations

import enum
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, List, Mapping, Optional, Sequence, Union
from uuid import UUID

import aiotools
import graphene
import sqlalchemy as sa
from dateutil.parser import parse as dtparse
from graphene.types.datetime import DateTime as GQLDateTime
from sqlalchemy.dialects import postgresql as pgsql
from sqlalchemy.ext.asyncio import AsyncSession as SASession
from sqlalchemy.orm import noload, relationship, selectinload

from ai.backend.common.types import (
    AccessKey,
    ClusterMode,
    ResourceSlot,
    SessionId,
    SessionResult,
    SessionTypes,
    SlotName,
    VFolderMount,
)

from ..api.exceptions import (
    MainKernelNotFound,
    SessionNotFound,
    TooManyKernelsFound,
    TooManySessionsMatched,
)
from ..defs import DEFAULT_ROLE
from .base import (
    GUID,
    Base,
    BigInt,
    EnumType,
    ForeignKeyIDColumn,
    Item,
    PaginatedList,
    ResourceSlotColumn,
    SessionIDColumn,
    StructuredJSONObjectListColumn,
    URLColumn,
    batch_multiresult_in_session,
    batch_result_in_session,
)
from .group import GroupRow
from .kernel import ComputeContainer, KernelRow, KernelStatus, get_user_email
from .minilang.ordering import QueryOrderParser
from .minilang.queryfilter import QueryFilterParser
from .user import UserRow

if TYPE_CHECKING:
    from sqlalchemy.engine import Row

    from .gql import GraphQueryContext


__all__ = (
    "aggregate_kernel_status",
    "SessionStatus",
    "DEAD_SESSION_STATUSES",
    "AGENT_RESOURCE_OCCUPYING_SESSION_STATUSES",
    "USER_RESOURCE_OCCUPYING_SESSION_STATUSES",
    "SessionRow",
    "SessionDependencyRow",
    "check_all_dependencies",
    "ComputeSession",
    "ComputeSessionList",
)


class SessionStatus(enum.Enum):
    # values are only meaningful inside the manager
    PENDING = 0
    # ---
    SCHEDULED = 5
    PREPARING = 10
    # ---
    RUNNING = 30
    RESTARTING = 31
    RUNNING_DEGRADED = 32
    # ---
    TERMINATING = 40
    TERMINATED = 41
    ERROR = 42
    CANCELLED = 43


DEAD_SESSION_STATUSES = (
    SessionStatus.CANCELLED,
    SessionStatus.TERMINATED,
)

# statuses to consider when calculating current resource usage
AGENT_RESOURCE_OCCUPYING_SESSION_STATUSES = tuple(
    e
    for e in SessionStatus
    if e
    not in (
        SessionStatus.TERMINATED,
        SessionStatus.PENDING,
        SessionStatus.CANCELLED,
    )
)

USER_RESOURCE_OCCUPYING_SESSION_STATUSES = tuple(
    e
    for e in SessionStatus
    if e
    not in (
        SessionStatus.TERMINATING,
        SessionStatus.TERMINATED,
        SessionStatus.PENDING,
        SessionStatus.CANCELLED,
    )
)


KERNEL_SESSION_STATUS_MAPPING: Mapping[KernelStatus, SessionStatus] = {
    KernelStatus.PENDING: SessionStatus.PENDING,
    KernelStatus.SCHEDULED: SessionStatus.SCHEDULED,
    KernelStatus.PREPARING: SessionStatus.PREPARING,
    KernelStatus.BUILDING: SessionStatus.PREPARING,
    KernelStatus.PULLING: SessionStatus.PREPARING,
    KernelStatus.RUNNING: SessionStatus.RUNNING,
    KernelStatus.RESTARTING: SessionStatus.RUNNING,
    KernelStatus.RESIZING: SessionStatus.RUNNING,
    KernelStatus.SUSPENDED: SessionStatus.RUNNING,
    KernelStatus.TERMINATING: SessionStatus.TERMINATING,
    KernelStatus.TERMINATED: SessionStatus.TERMINATED,
    KernelStatus.ERROR: SessionStatus.ERROR,
    KernelStatus.CANCELLED: SessionStatus.CANCELLED,
}


def aggregate_kernel_status(
    kernels: Sequence[KernelRow], target_status: KernelStatus
) -> SessionStatus:
    candidate: SessionStatus = KERNEL_SESSION_STATUS_MAPPING[target_status]
    priority_statuses = [status for status in KernelStatus if status.value < target_status.value]
    for s in kernels:
        if s.status == KernelStatus.ERROR:
            if s.cluster_role == DEFAULT_ROLE:
                return SessionStatus.ERROR
            return SessionStatus.RUNNING_DEGRADED
        elif s.status in priority_statuses:
            candidate = KERNEL_SESSION_STATUS_MAPPING[s]
        elif s.status == target_status:
            continue
        else:
            # TODO: handle unexpected kernel status
            pass
    return candidate


def _build_session_fetch_query(
    base_cond,
    access_key: AccessKey | None = None,
    *,
    max_matches: int | None,
    allow_stale: bool = True,
    for_update: bool = False,
    do_ordering: bool = False,
    eager_loading_op: Iterable | None = None,
):
    cond = base_cond
    if access_key is not None:
        cond = cond & (SessionRow.access_key == access_key)
    if not allow_stale:
        cond = cond & (~SessionRow.status.in_(DEAD_SESSION_STATUSES))
    query = (
        sa.select(SessionRow)
        .where(cond)
        .order_by(sa.desc(SessionRow.created_at))
        .execution_options(populate_existing=True)
    )
    if max_matches:
        query = query.limit(max_matches).offset(0)
    if for_update:
        query = query.with_for_update()
    if do_ordering:
        query = query.order_by(SessionRow.created_at)

    if eager_loading_op is not None:
        query = query.options(*eager_loading_op)

    return query


async def _match_sessions_by_id(
    db_session: SASession,
    session_id: SessionId,
    access_key: AccessKey | None = None,
    *,
    max_matches: int | None = None,
    allow_prefix: bool = False,
    allow_stale: bool = True,
    for_update: bool = False,
    eager_loading_op=None,
) -> List[SessionRow]:
    if allow_prefix:
        cond = sa.sql.expression.cast(SessionRow.id, sa.String).like(f"{session_id}%")
    else:
        cond = SessionRow.id == session_id
    query = _build_session_fetch_query(
        cond,
        access_key,
        max_matches=max_matches,
        allow_stale=allow_stale,
        for_update=for_update,
        eager_loading_op=eager_loading_op,
    )

    result = await db_session.execute(query)
    return result.scalars().all()


async def _match_sessions_by_name(
    db_session: SASession,
    session_name: str,
    access_key: AccessKey,
    *,
    max_matches: int | None,
    allow_prefix: bool = False,
    allow_stale: bool = True,
    for_update: bool = False,
    eager_loading_op=None,
) -> List[SessionRow]:
    if allow_prefix:
        cond = sa.sql.expression.cast(SessionRow.name, sa.String).like(f"{session_name}%")
    else:
        cond = SessionRow.name == session_name
    query = _build_session_fetch_query(
        cond,
        access_key,
        max_matches=max_matches,
        allow_stale=allow_stale,
        for_update=for_update,
        eager_loading_op=eager_loading_op,
    )
    result = await db_session.execute(query)
    return result.scalars().all()


class SessionOp(str, enum.Enum):
    CREATE = "create_session"
    DESTROY = "destroy_session"
    RESTART = "restart_session"
    EXECUTE = "execute"
    REFRESH = "refresh_session"
    SHUTDOWN_SERVICE = "shutdown_service"
    UPLOAD_FILE = "upload_file"
    DOWNLOAD_FILE = "download_file"
    LIST_FILE = "list_files"
    GET_AGENT_LOGS = "get_logs_from_agent"


class SessionRow(Base):
    __tablename__ = "sessions"
    id = SessionIDColumn()
    creation_id = sa.Column("creation_id", sa.String(length=32), unique=False, index=False)
    name = sa.Column("name", sa.String(length=64), unique=False, index=True)
    session_type = sa.Column(
        "session_type",
        EnumType(SessionTypes),
        index=True,
        nullable=False,  # previously sess_type
        default=SessionTypes.INTERACTIVE,
        server_default=SessionTypes.INTERACTIVE.name,
    )

    cluster_mode = sa.Column(
        "cluster_mode",
        sa.String(length=16),
        nullable=False,
        default=ClusterMode.SINGLE_NODE,
        server_default=ClusterMode.SINGLE_NODE.name,
    )
    cluster_size = sa.Column("cluster_size", sa.Integer, nullable=False, default=1)
    kernels = relationship("KernelRow", back_populates="session")

    # Resource ownership
    scaling_group_name = sa.Column(
        "scaling_group_name", sa.ForeignKey("scaling_groups.name"), index=True, nullable=True
    )
    scaling_group = relationship("ScalingGroupRow", back_populates="sessions")
    target_sgroup_names = sa.Column(
        "target_sgroup_names",
        sa.ARRAY(sa.String(length=64)),
        default="{}",
        server_default="{}",
        nullable=True,
    )
    domain_name = sa.Column(
        "domain_name", sa.String(length=64), sa.ForeignKey("domains.name"), nullable=False
    )
    domain = relationship("DomainRow", back_populates="sessions")
    group_id = ForeignKeyIDColumn("group_id", "groups.id", nullable=False)
    group = relationship("GroupRow", back_populates="sessions")
    user_uuid = ForeignKeyIDColumn("user_uuid", "users.uuid", nullable=False)
    user = relationship("UserRow", back_populates="sessions")
    access_key = sa.Column("access_key", sa.String(length=20), sa.ForeignKey("keypairs.access_key"))
    access_key_row = relationship("KeyPairRow", back_populates="sessions")

    # if image_id is null, should find a image field from related kernel row.
    image_id = ForeignKeyIDColumn("image_id", "images.id")
    # `image` column is identical to kernels `image` column.
    image = sa.Column("image", sa.String(length=512))
    image_row = relationship("ImageRow", back_populates="sessions")
    tag = sa.Column("tag", sa.String(length=64), nullable=True)

    # Resource occupation
    # occupied_slots = sa.Column('occupied_slots', ResourceSlotColumn(), nullable=False)
    occupying_slots = sa.Column("occupying_slots", ResourceSlotColumn(), nullable=False)
    requested_slots = sa.Column("requested_slots", ResourceSlotColumn(), nullable=False)
    vfolder_mounts = sa.Column(
        "vfolder_mounts", StructuredJSONObjectListColumn(VFolderMount), nullable=True
    )
    resource_opts = sa.Column("resource_opts", pgsql.JSONB(), nullable=True, default={})
    environ = sa.Column("environ", pgsql.JSONB(), nullable=True, default={})
    bootstrap_script = sa.Column("bootstrap_script", sa.String(length=16 * 1024), nullable=True)

    # Lifecycle
    created_at = sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
    )
    terminated_at = sa.Column(
        "terminated_at", sa.DateTime(timezone=True), nullable=True, default=sa.null(), index=True
    )
    starts_at = sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True, default=sa.null())
    status = sa.Column(
        "status",
        EnumType(SessionStatus),
        default=SessionStatus.PENDING,
        server_default=SessionStatus.PENDING.name,
        nullable=False,
        index=True,
    )
    status_changed = sa.Column(
        "status_changed", sa.DateTime(timezone=True), nullable=True, index=True
    )
    status_info = sa.Column("status_info", sa.Unicode(), nullable=True, default=sa.null())
    status_data = sa.Column("status_data", pgsql.JSONB(), nullable=True, default=sa.null())
    # status_data contains a JSON object that contains detailed data for the last status change.
    # During scheduling (as PENDING + ("no-available-instances" | "predicate-checks-failed")):
    # {
    #   "scheduler": {
    #     // shceudler attempt information
    #     // NOTE: the whole field may be NULL before the first attempt!
    #     "retries": 5,
    #         // the number of scheudling attempts (used to avoid HoL blocking as well)
    #     "last_try": "2021-05-01T12:34:56.123456+09:00",
    #         // an ISO 8601 formatted timestamp of the last attempt
    #     "failed_predicates": [
    #       { "name": "concurrency", "msg": "You cannot run more than 30 concurrent sessions." },
    #           // see the manager.scheduler.predicates module for possible messages
    #       ...
    #     ],
    #     "passed_predicates": [ {"name": "reserved_time"}, ... ],  // names only
    #   }
    # }
    #
    # While running: the field is NULL.
    #
    # After termination:
    # {
    #   "kernel": {
    #     // termination info for the individual kernel
    #     "exit_code": 123,
    #         // maybe null during termination
    #   },
    #   "session": {
    #     // termination info for the session
    #     "status": "terminating" | "terminated"
    #         // "terminated" means all kernels that belong to the same session has terminated.
    #         // used to prevent duplication of SessionTerminatedEvent
    #   }
    # }
    status_history = sa.Column("status_history", pgsql.JSONB(), nullable=True, default=sa.null())
    callback_url = sa.Column("callback_url", URLColumn, nullable=True, default=sa.null())

    startup_command = sa.Column("startup_command", sa.Text, nullable=True)
    result = sa.Column(
        "result",
        EnumType(SessionResult),
        default=SessionResult.UNDEFINED,
        server_default=SessionResult.UNDEFINED.name,
        nullable=False,
        index=True,
    )

    # Resource metrics measured upon termination
    num_queries = sa.Column("num_queries", sa.BigInteger(), default=0)
    last_stat = sa.Column("last_stat", pgsql.JSONB(), nullable=True, default=sa.null())

    __table_args__ = (
        # indexing
        sa.Index(
            "ix_sessions_updated_order",
            sa.func.greatest("created_at", "terminated_at", "status_changed"),
            unique=False,
        ),
    )

    @property
    def main_kernel(self) -> KernelRow:
        kerns = tuple(kern for kern in self.kernels if kern.cluster_role == DEFAULT_ROLE)
        if len(kerns) > 1:
            raise TooManyKernelsFound(
                f"Session (id: {self.id}) " "has more than 1 main kernel.",
            )
        if len(kerns) == 0:
            raise MainKernelNotFound(
                f"Session (id: {self.id}) has no main kernel.",
            )
        return kerns[0]

    @classmethod
    async def match_sessions(
        cls,
        db_session: SASession,
        session_name_or_id: Union[str, UUID],
        access_key: AccessKey | None,
        *,
        allow_prefix: bool = False,
        allow_stale: bool = True,
        for_update: bool = False,
        max_matches: int = 10,
        eager_loading_op=None,
    ) -> List[SessionRow]:
        """
        Match the prefix of session ID or session name among the sessions
        that belongs to the given access key, and return the list of SessionRow.
        """

        query_list = [
            aiotools.apartial(
                _match_sessions_by_name,
                session_name=str(session_name_or_id),
                allow_prefix=allow_prefix,
            )
        ]
        try:
            UUID(str(session_name_or_id))
        except ValueError:
            pass
        else:
            # Fetch id-based query first
            assert isinstance(session_name_or_id, UUID)
            query_list = [
                aiotools.apartial(
                    _match_sessions_by_id,
                    session_id=SessionId(session_name_or_id),
                    allow_prefix=False,
                ),
                *query_list,
            ]
            if allow_prefix:
                query_list = [
                    aiotools.apartial(
                        _match_sessions_by_id,
                        session_id=SessionId(session_name_or_id),
                        allow_prefix=True,
                    ),
                    *query_list,
                ]

        for fetch_func in query_list:
            rows = await fetch_func(
                db_session,
                access_key=access_key,
                allow_stale=allow_stale,
                for_update=for_update,
                max_matches=max_matches,
                eager_loading_op=eager_loading_op,
            )
            if not rows:
                continue
            return rows
        return []

    @classmethod
    async def get_session(
        cls,
        session_name_or_id: Union[str, UUID],
        access_key: AccessKey | None = None,
        *,
        allow_stale: bool = False,
        for_update: bool = False,
        db_session: SASession,
    ) -> SessionRow:
        """
        Retrieve the session information by session's UUID,
        or session's name paired with access_key.
        This will return the information of the session and the sibling kernel(s).

        :param session_name_or_id: session's id or session's name.
        :param access_key: Access key used to create session.
        :param allow_stale: If True, filter "inactive" sessions as well as "active" ones.
                            If False, filter "active" sessions only.
        :param for_update: Apply for_update during select query.
        :param db_session: Database connection for reuse.
        """
        session_list = await cls.match_sessions(
            db_session,
            session_name_or_id,
            access_key,
            allow_stale=allow_stale,
            for_update=for_update,
        )
        if not session_list:
            raise SessionNotFound(f"Session (id={session_name_or_id}) does not exist.")
        if len(session_list) > 1:
            session_infos = [
                {
                    "session_id": sess.id,
                    "session_name": sess.name,
                    "status": sess.status,
                    "created_at": sess.created_at,
                }
                for sess in session_list
            ]
            raise TooManySessionsMatched(extra_data={"matches": session_infos})
        return session_list[0]

    @classmethod
    async def get_session_with_kernels(
        cls,
        session_name_or_id: str | UUID,
        access_key: AccessKey | None = None,
        *,
        allow_stale: bool = False,
        for_update: bool = False,
        only_main_kern: bool = False,
        db_session: SASession,
    ) -> SessionRow:
        kernel_rel = SessionRow.kernels
        if only_main_kern:
            kernel_rel.and_(KernelRow.cluster_role == DEFAULT_ROLE)
        kernel_loading_op = (
            noload("*"),
            selectinload(kernel_rel).options(
                noload("*"),
                selectinload(KernelRow.image_row).noload("*"),
                selectinload(KernelRow.agent_row).noload("*"),
            ),
        )
        session_list = await cls.match_sessions(
            db_session,
            session_name_or_id,
            access_key,
            allow_stale=allow_stale,
            for_update=for_update,
            eager_loading_op=kernel_loading_op,
        )
        try:
            return session_list[0]
        except IndexError:
            raise SessionNotFound(f"Session (id={session_name_or_id}) does not exist.")

    @classmethod
    async def get_session_with_main_kernel(
        cls,
        session_name_or_id: str | UUID,
        access_key: AccessKey | None = None,
        *,
        allow_stale: bool = False,
        for_update: bool = False,
        db_session: SASession,
    ) -> SessionRow:
        return await cls.get_session_with_kernels(
            session_name_or_id,
            access_key,
            allow_stale=allow_stale,
            for_update=for_update,
            only_main_kern=True,
            db_session=db_session,
        )

    @classmethod
    async def get_session_by_id(
        cls,
        db_session: SASession,
        session_id: SessionId,
        access_key: AccessKey | None = None,
        *,
        max_matches: int | None = None,
        allow_stale: bool = True,
        for_update: bool = False,
        eager_loading_op=None,
    ) -> SessionRow:
        sessions = await _match_sessions_by_id(
            db_session,
            session_id,
            access_key,
            max_matches=max_matches,
            allow_stale=allow_stale,
            for_update=for_update,
            eager_loading_op=eager_loading_op,
            allow_prefix=False,
        )
        try:
            return sessions[0]
        except IndexError:
            raise SessionNotFound(f"Session (id={session_id}) does not exist.")

    @classmethod
    async def get_sgroup_managed_sessions(
        cls,
        db_sess: SASession,
        sgroup_name: str,
    ) -> List[SessionRow]:
        candidate_statues = (SessionStatus.PENDING, *AGENT_RESOURCE_OCCUPYING_SESSION_STATUSES)
        query = (
            sa.select(SessionRow)
            .where(
                (SessionRow.scaling_group_name == sgroup_name)
                & (SessionRow.status.in_(candidate_statues))
            )
            .options(
                noload("*"),
                selectinload(SessionRow.group).options(noload("*")),
                selectinload(SessionRow.domain).options(noload("*")),
                selectinload(SessionRow.access_key_row).options(noload("*")),
                selectinload(SessionRow.kernels).options(
                    noload("*"), selectinload(KernelRow.image_row).options(noload("*"))
                ),
            )
        )
        result = await db_sess.execute(query)
        return result.scalars().all()


class SessionDependencyRow(Base):
    __tablename__ = "session_dependencies"
    session_id = sa.Column(
        "session_id",
        GUID,
        sa.ForeignKey("sessions.id", onupdate="CASCADE", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    depends_on = sa.Column(
        "depends_on",
        GUID,
        sa.ForeignKey("sessions.id", onupdate="CASCADE", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    __table_args__ = (
        # constraint
        sa.PrimaryKeyConstraint("session_id", "depends_on", name="sess_dep_pk"),
    )


async def check_all_dependencies(
    db_session: SASession,
    sess_ctx: SessionRow,
) -> List[SessionRow]:
    j = sa.join(
        SessionDependencyRow,
        SessionRow,
        SessionDependencyRow.depends_on == SessionDependencyRow.session_id,
    )
    query = (
        sa.select(SessionRow.id, SessionRow.name, SessionRow.result)
        .select_from(j)
        .where(SessionDependencyRow.session_id == sess_ctx.id)
    )
    result = await db_session.execute(query)
    rows = result.scalars().all()
    pending_dependencies = [
        sess_row for sess_row in rows if sess_row.result != SessionResult.SUCCESS
    ]
    return pending_dependencies


DEFAULT_SESSION_ORDERING = [
    sa.desc(
        sa.func.greatest(
            SessionRow.created_at,
            SessionRow.terminated_at,
            SessionRow.status_changed,
        )
    ),
]


class ComputeSession(graphene.ObjectType):
    class Meta:
        interfaces = (Item,)

    # identity
    tag = graphene.String()
    name = graphene.String()
    type = graphene.String()
    session_id = graphene.UUID()

    # image
    image = graphene.String()  # image for the main container
    architecture = graphene.String()  # image architecture for the main container
    registry = graphene.String()  # image registry for the main container
    cluster_template = graphene.String()
    cluster_mode = graphene.String()
    cluster_size = graphene.Int()

    # ownership
    domain_name = graphene.String()
    group_name = graphene.String()
    group_id = graphene.UUID()
    user_email = graphene.String()
    user_id = graphene.UUID()
    access_key = graphene.String()
    created_user_email = graphene.String()
    created_user_id = graphene.UUID()

    # status
    status = graphene.String()
    status_changed = GQLDateTime()
    status_info = graphene.String()
    status_data = graphene.JSONString()
    status_history = graphene.JSONString()
    created_at = GQLDateTime()
    terminated_at = GQLDateTime()
    starts_at = GQLDateTime()
    startup_command = graphene.String()
    result = graphene.String()
    commit_status = graphene.String()
    abusing_reports = graphene.List(lambda: graphene.JSONString)

    # resources
    resource_opts = graphene.JSONString()
    scaling_group = graphene.String()
    service_ports = graphene.JSONString()
    mounts = graphene.List(lambda: graphene.String)
    occupying_slots = graphene.JSONString()

    # statistics
    num_queries = BigInt()

    # owned containers (aka kernels)
    containers = graphene.List(lambda: ComputeContainer)

    # relations
    dependencies = graphene.List(lambda: ComputeSession)

    @classmethod
    def parse_row(cls, ctx: GraphQueryContext, row: Row) -> Mapping[str, Any]:
        assert row is not None
        email = getattr(row, "email")
        group_name = getattr(row, "group_name")
        row = row.SessionRow
        return {
            # identity
            "session_id": row.id,
            "id": row.main_kernel.id,
            "tag": row.tag,
            "name": row.name,
            "type": row.session_type.name,
            # image
            # "image": row.image_id,
            "image": row.image,
            "architecture": row.main_kernel.architecture,
            "registry": row.main_kernel.registry,
            "cluster_template": None,  # TODO: implement
            "cluster_mode": row.cluster_mode,
            "cluster_size": row.cluster_size,
            # ownership
            "domain_name": row.domain_name,
            "group_name": group_name,
            "group_id": row.group_id,
            "user_email": email,
            "user_id": row.user_uuid,
            "access_key": row.access_key,
            "created_user_email": None,  # TODO: implement
            "created_user_id": None,  # TODO: implement
            # status
            "status": row.status.name,
            "status_changed": row.status_changed,
            "status_info": row.status_info,
            "status_data": row.status_data,
            "status_history": row.status_history or {},
            "created_at": row.created_at,
            "terminated_at": row.terminated_at,
            "starts_at": row.starts_at,
            "startup_command": row.startup_command,
            "result": row.result.name,
            # resources
            "resource_opts": row.resource_opts,
            "scaling_group": row.scaling_group_name,
            "service_ports": row.main_kernel.service_ports,
            # statistics
            "num_queries": row.num_queries,
        }

    @classmethod
    def from_row(cls, ctx: GraphQueryContext, row: Row) -> ComputeSession | None:
        if row is None:
            return None
        props = cls.parse_row(ctx, row)
        return cls(**props)

    async def resolve_occupying_slots(self, info: graphene.ResolveInfo) -> Mapping[str, Any]:
        """
        Calculate the sum of occupying resource slots of all sub-kernels,
        and return the JSON-serializable object from the sum result.
        """
        graph_ctx: GraphQueryContext = info.context
        loader = graph_ctx.dataloader_manager.get_loader(graph_ctx, "ComputeContainer.by_session")
        containers = await loader.load(self.session_id)
        zero = ResourceSlot()
        return sum(
            (
                ResourceSlot({SlotName(k): Decimal(v) for k, v in c.occupied_slots.items()})
                for c in containers
            ),
            start=zero,
        ).to_json()

    async def resolve_containers(
        self,
        info: graphene.ResolveInfo,
    ) -> Iterable[ComputeContainer]:
        graph_ctx: GraphQueryContext = info.context
        loader = graph_ctx.dataloader_manager.get_loader(graph_ctx, "ComputeContainer.by_session")
        return await loader.load(self.session_id)

    async def resolve_dependencies(
        self,
        info: graphene.ResolveInfo,
    ) -> Iterable[ComputeSession]:
        graph_ctx: GraphQueryContext = info.context
        loader = graph_ctx.dataloader_manager.get_loader(graph_ctx, "ComputeSession.by_dependency")
        return await loader.load(self.id)

    async def resolve_commit_status(self, info: graphene.ResolveInfo) -> Optional[str]:
        graph_ctx: GraphQueryContext = info.context
        if self.status != "RUNNING":
            return None
        async with graph_ctx.db.begin_readonly_session() as db_sess:
            query = sa.select(KernelRow).where(
                (KernelRow.session_id == self.id) & (KernelRow.cluster_role == DEFAULT_ROLE)
            )
            kernel = (await db_sess.execute(query)).scalars().first()
            email = await get_user_email(db_sess, kernel)

        commit_status = await graph_ctx.registry.get_commit_status(
            kernel.id, kernel.agent, kernel.agent_addr, self.id, sub_dir=email
        )
        return commit_status["status"]

    async def resolve_abusing_reports(
        self, info: graphene.ResolveInfo
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        containers = self.containers
        if containers is None:
            containers = await self.resolve_containers(info)
        if containers is None:
            return []
        return [(await con.resolve_abusing_report(info, self.access_key)) for con in containers]

    _queryfilter_fieldspec = {
        "type": ("kernels_session_type", lambda s: SessionTypes[s]),
        "name": ("kernels_session_name", None),
        "image": ("kernels_image", None),
        "architecture": ("kernels_architecture", None),
        "domain_name": ("kernels_domain_name", None),
        "group_name": ("groups_group_name", None),
        "user_email": ("users_email", None),
        "access_key": ("kernels_access_key", None),
        "scaling_group": ("kernels_scaling_group", None),
        "cluster_mode": ("kernels_cluster_mode", lambda s: ClusterMode[s]),
        "cluster_template": ("kernels_cluster_template", None),
        "cluster_size": ("kernels_cluster_size", None),
        "status": ("kernels_status", lambda s: KernelStatus[s]),
        "status_info": ("kernels_status_info", None),
        "status_changed": ("kernels_status_changed", dtparse),
        "result": ("kernels_result", lambda s: SessionResult[s]),
        "created_at": ("kernels_created_at", dtparse),
        "terminated_at": ("kernels_terminated_at", dtparse),
        "starts_at": ("kernels_starts_at", dtparse),
        "startup_command": ("kernels_startup_command", None),
        "agent": ("kernels_agent", None),
        "agents": ("kernels_agent", None),
    }

    _queryorder_colmap = {
        "id": "kernels_id",
        "type": "kernels_session_type",
        "name": "kernels_session_name",
        "image": "kernels_image",
        "architecture": "kernels_architecture",
        "domain_name": "kernels_domain_name",
        "group_name": "kernels_group_name",
        "user_email": "users_email",
        "access_key": "kernels_access_key",
        "scaling_group": "kernels_scaling_group",
        "cluster_mode": "kernels_cluster_mode",
        "cluster_template": "kernels_cluster_template",
        "cluster_size": "kernels_cluster_size",
        "status": "kernels_status",
        "status_info": "kernels_status_info",
        "status_changed": "kernels_status_info",
        "result": "kernels_result",
        "created_at": "kernels_created_at",
        "terminated_at": "kernels_terminated_at",
        "starts_at": "kernels_starts_at",
    }

    @classmethod
    async def load_count(
        cls,
        ctx: GraphQueryContext,
        *,
        domain_name: str = None,
        group_id: UUID = None,
        access_key: str = None,
        status: str = None,
        filter: str = None,
    ) -> int:
        if isinstance(status, str):
            status_list = [SessionStatus[s] for s in status.split(",")]
        elif isinstance(status, SessionStatus):
            status_list = [status]
        j = sa.join(SessionRow, GroupRow, SessionRow.group_id == GroupRow.id).join(
            UserRow, SessionRow.user_uuid == UserRow.uuid
        )
        query = sa.select([sa.func.count()]).select_from(j)
        if domain_name is not None:
            query = query.where(SessionRow.domain_name == domain_name)
        if group_id is not None:
            query = query.where(SessionRow.group_id == group_id)
        if access_key is not None:
            query = query.where(SessionRow.access_key == access_key)
        if status is not None:
            query = query.where(SessionRow.status.in_(status_list))
        if filter is not None:
            qfparser = QueryFilterParser(cls._queryfilter_fieldspec)
            query = qfparser.append_filter(query, filter)
        async with ctx.db.begin_readonly() as conn:
            result = await conn.execute(query)
            return result.scalar()

    @classmethod
    async def load_slice(
        cls,
        ctx: GraphQueryContext,
        limit: int,
        offset: int,
        *,
        domain_name: str = None,
        group_id: UUID = None,
        access_key: str = None,
        status: str = None,
        filter: str = None,
        order: str = None,
    ) -> Sequence[ComputeSession | None]:
        if isinstance(status, str):
            status_list = [SessionStatus[s] for s in status.split(",")]
        elif isinstance(status, SessionStatus):
            status_list = [status]
        j = sa.join(SessionRow, GroupRow, SessionRow.group_id == GroupRow.id).join(
            UserRow, SessionRow.user_uuid == UserRow.uuid
        )
        query = (
            sa.select(
                SessionRow,
                GroupRow.name.label("group_name"),
                UserRow.email,
            )
            .select_from(j)
            .options(selectinload(SessionRow.kernels))
            .limit(limit)
            .offset(offset)
        )
        if domain_name is not None:
            query = query.where(SessionRow.domain_name == domain_name)
        if group_id is not None:
            query = query.where(SessionRow.group_id == group_id)
        if access_key is not None:
            query = query.where(SessionRow.access_key == access_key)
        if status is not None:
            query = query.where(SessionRow.status.in_(status_list))
        if filter is not None:
            parser = QueryFilterParser(cls._queryfilter_fieldspec)
            query = parser.append_filter(query, filter)
        if order is not None:
            qoparser = QueryOrderParser(cls._queryorder_colmap)
            query = qoparser.append_ordering(query, order)
        else:
            query = query.order_by(*DEFAULT_SESSION_ORDERING)
        async with ctx.db.begin_readonly_session() as db_sess:
            return [cls.from_row(ctx, r) async for r in (await db_sess.stream(query))]

    @classmethod
    async def batch_load_detail(
        cls,
        ctx: GraphQueryContext,
        session_ids: Sequence[SessionId],
        *,
        domain_name: str = None,
        access_key: str = None,
    ) -> Sequence[ComputeSession | None]:
        j = sa.join(SessionRow, GroupRow, SessionRow.group_id == GroupRow.id).join(
            UserRow, SessionRow.user_uuid == UserRow.uuid
        )
        query = (
            sa.select(
                SessionRow,
                GroupRow.name.label("group_name"),
                UserRow.email,
            )
            .select_from(j)
            .where(SessionRow.id.in_(session_ids))
        )
        if domain_name is not None:
            query = query.where(SessionRow.domain_name == domain_name)
        if access_key is not None:
            query = query.where(SessionRow.access_key == access_key)
        async with ctx.db.begin_readonly_session() as db_sess:
            return await batch_result_in_session(
                ctx,
                db_sess,
                query,
                cls,
                session_ids,
                lambda row: row["id"],
            )

    @classmethod
    async def batch_load_by_dependency(
        cls,
        ctx: GraphQueryContext,
        session_ids: Sequence[SessionId],
    ) -> Sequence[Sequence[ComputeSession]]:
        j = sa.join(
            SessionRow,
            SessionDependencyRow,
            SessionRow.id == SessionDependencyRow.depends_on,
        )
        query = (
            sa.select(SessionRow)
            .select_from(j)
            .where(SessionDependencyRow.session_id.in_(session_ids))
        )
        async with ctx.db.begin_readonly_session() as db_sess:
            return await batch_multiresult_in_session(
                ctx,
                db_sess,
                query,
                cls,
                session_ids,
                lambda row: row["id"],
            )


class ComputeSessionList(graphene.ObjectType):
    class Meta:
        interfaces = (PaginatedList,)

    items = graphene.List(ComputeSession, required=True)
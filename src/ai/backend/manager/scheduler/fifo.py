from __future__ import annotations
from decimal import Decimal
from typing import (
    List,
    Optional,
    Sequence,
    Tuple,
)

import trafaret as t

from ai.backend.common.types import (
    AgentId,
    ResourceSlot,
    SessionId,
)
from .types import (
    AbstractScheduler,
    AgentContext,
    PendingSession,
    ExistingSession,
    KernelInfo,
)
from ..models import SessionRow, AgentRow, KernelRow


def key_by_requested_slots(
    agent: AgentRow,
    requested_slots: ResourceSlot,
) -> Tuple[int, ResourceSlot]:
    unused_slot_keys = set()
    for k, v in requested_slots.items():
        if v == Decimal(0):
            unused_slot_keys.add(k)
    num_extras = 0
    for k, v in agent.available_slots.items():
        if k in unused_slot_keys and v > Decimal(0):
            num_extras += 1
    # Put back agents with more extra slot types
    # (e.g., accelerators)
    # Also put front agents with exactly required slot types
    return (-num_extras, agent.available_slots)


class FIFOSlotScheduler(AbstractScheduler):

    config_iv = t.Dict({
        t.Key('num_retries_to_skip', default=0): t.ToInt(gte=0),
    }).allow_extra('*')

    def pick_session(
        self,
        total_capacity: ResourceSlot,
        pending_sessions: Sequence[SessionRow],
        existing_sessions: Sequence[SessionRow],
    ) -> Optional[SessionId]:
        local_pending_sessions = list(pending_sessions)
        skipped_sessions: List[SessionRow] = []
        max_retries = self.config['num_retries_to_skip']
        for s in local_pending_sessions:
            # Just pick the first pending session, but skip it
            # if it has more than 3 failures.
            if max_retries == 0:
                return s
            if s.status_data is not None:
                sched_data = s.status_data.get('scheduler', {})
                if sched_data.get('retries', 0) >= max_retries:
                    skipped_sessions.append(s)
                    continue
            return s
        # But if all sessions are skipped, then choose the first one.
        if skipped_sessions:
            return skipped_sessions[0]
        return None

    def _assign_agent(
        self,
        agents: Sequence[AgentRow],
        requested_slots: ResourceSlot,
    ) -> Optional[AgentRow]:
        possible_agents = []
        for agent in agents:
            remaining_slots = agent.available_slots - agent.occupied_slots
            if remaining_slots >= requested_slots:
                possible_agents.append(agent)
        if possible_agents:
            chosen_agent = max(
                possible_agents,
                key=lambda a: key_by_requested_slots(
                    a,
                    requested_slots,
                ),
            )
            return chosen_agent
        return None

    def assign_agent_for_session(
        self,
        agents: Sequence[AgentRow],
        pending_session: SessionRow,
    ) -> Optional[AgentId]:
        return self._assign_agent(
            agents, pending_session.requested_slots,
        )

    def assign_agent_for_kernel(
        self,
        agents: Sequence[AgentRow],
        pending_kernel: KernelRow,
    ) -> Optional[AgentId]:
        return self._assign_agent(
            agents, pending_kernel.requested_slots,
        )


class LIFOSlotScheduler(AbstractScheduler):

    config_iv = t.Dict({}).allow_extra('*')

    def pick_session(
        self,
        total_capacity: ResourceSlot,
        pending_sessions: Sequence[SessionRow],
        existing_sessions: Sequence[SessionRow],
    ) -> Optional[SessionRow]:
        # Just pick the last pending session.
        return pending_sessions[-1]

    def _assign_agent(
        self,
        agents: Sequence[AgentRow],
        requested_slots: ResourceSlot,
    ) -> Optional[AgentRow]:
        possible_agents = []
        for agent in agents:
            remaining_slots = agent.available_slots - agent.occupied_slots
            if remaining_slots >= requested_slots:
                possible_agents.append(agent)
        if possible_agents:
            chosen_agent = max(
                possible_agents,
                key=lambda a: key_by_requested_slots(
                    a,
                    requested_slots,
                ),
            )
            return chosen_agent
        return None

    def assign_agent_for_session(
        self,
        agents: Sequence[AgentRow],
        pending_session: SessionRow,
    ) -> Optional[AgentRow]:
        return self._assign_agent(
            agents, pending_session.requested_slots,
        )

    def assign_agent_for_kernel(
        self,
        agents: Sequence[AgentRow],
        pending_kernel: KernelRow,
    ) -> Optional[AgentRow]:
        return self._assign_agent(
            agents, pending_kernel.requested_slots,
        )
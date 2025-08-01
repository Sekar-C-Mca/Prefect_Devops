"""
Functions for interacting with flow run ORM objects.
Intended for internal use by the Prefect REST API.
"""

import contextlib
import datetime
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from sqlalchemy.sql import Select

import prefect.server.models as models
import prefect.server.schemas as schemas
from prefect.logging.loggers import get_logger
from prefect.server.database import PrefectDBInterface, db_injector, orm_models
from prefect.server.exceptions import ObjectNotFoundError
from prefect.server.orchestration.core_policy import MinimalFlowPolicy
from prefect.server.orchestration.global_policy import GlobalFlowPolicy
from prefect.server.orchestration.policies import (
    FlowRunOrchestrationPolicy,
)
from prefect.server.orchestration.rules import FlowOrchestrationContext
from prefect.server.schemas.core import TaskRunResult
from prefect.server.schemas.graph import Graph
from prefect.server.schemas.responses import OrchestrationResult
from prefect.server.schemas.states import State
from prefect.server.utilities.schemas import PrefectBaseModel
from prefect.settings import (
    PREFECT_API_MAX_FLOW_RUN_GRAPH_ARTIFACTS,
    PREFECT_API_MAX_FLOW_RUN_GRAPH_NODES,
)
from prefect.types import KeyValueLabels
from prefect.types._datetime import DateTime, earliest_possible_datetime, now

if TYPE_CHECKING:
    import logging

logger: "logging.Logger" = get_logger("flow_runs")


T = TypeVar("T", bound=tuple[Any, ...])


@db_injector
async def create_flow_run(
    db: PrefectDBInterface,
    session: AsyncSession,
    flow_run: schemas.core.FlowRun,
    orchestration_parameters: Optional[dict[str, Any]] = None,
) -> orm_models.FlowRun:
    """Creates a new flow run.

    If the provided flow run has a state attached, it will also be created.

    Args:
        session: a database session
        flow_run: a flow run model

    Returns:
        orm_models.FlowRun: the newly-created flow run
    """
    right_now = now("UTC")
    # model: Union[orm_models.FlowRun, None] = None

    flow_run.labels = await with_system_labels_for_flow_run(
        session=session, flow_run=flow_run
    )

    flow_run_dict = dict(
        **flow_run.model_dump_for_orm(
            exclude={
                "created",
                "state",
                "estimated_run_time",
                "estimated_start_time_delta",
            },
            exclude_unset=True,
        ),
        created=right_now,
    )

    # if no idempotency key was provided, create the run directly
    if not flow_run.idempotency_key:
        model = db.FlowRun(**flow_run_dict)
        session.add(model)
        await session.flush()

    # otherwise let the database take care of enforcing idempotency
    else:
        insert_stmt = (
            db.queries.insert(db.FlowRun)
            .values(**flow_run_dict)
            .on_conflict_do_nothing(
                index_elements=db.orm.flow_run_unique_upsert_columns,
            )
        )
        await session.execute(insert_stmt)

        # read the run to see if idempotency was applied or not
        query = (
            sa.select(db.FlowRun)
            .where(
                sa.and_(
                    db.FlowRun.flow_id == flow_run.flow_id,
                    db.FlowRun.idempotency_key == flow_run.idempotency_key,
                )
            )
            .limit(1)
            .execution_options(populate_existing=True)
            .options(
                selectinload(db.FlowRun.work_queue).selectinload(db.WorkQueue.work_pool)
            )
        )
        result = await session.execute(query)
        model = result.scalar_one()

    # if the flow run was created in this function call then we need to set the
    # state. If it was created idempotently, the created time won't match.
    if model.created == right_now and flow_run.state:
        await models.flow_runs.set_flow_run_state(
            session=session,
            flow_run_id=model.id,
            state=flow_run.state,
            force=True,
            orchestration_parameters=orchestration_parameters,
        )
    return model


@db_injector
async def update_flow_run(
    db: PrefectDBInterface,
    session: AsyncSession,
    flow_run_id: UUID,
    flow_run: schemas.actions.FlowRunUpdate,
) -> bool:
    """
    Updates a flow run.

    Args:
        session: a database session
        flow_run_id: the flow run id to update
        flow_run: a flow run model

    Returns:
        bool: whether or not matching rows were found to update
    """
    update_stmt = (
        sa.update(db.FlowRun)
        .where(db.FlowRun.id == flow_run_id)
        # exclude_unset=True allows us to only update values provided by
        # the user, ignoring any defaults on the model
        .values(**flow_run.model_dump_for_orm(exclude_unset=True))
    )
    result = await session.execute(update_stmt)
    return result.rowcount > 0


@db_injector
async def read_flow_run(
    db: PrefectDBInterface,
    session: AsyncSession,
    flow_run_id: UUID,
    for_update: bool = False,
) -> Optional[orm_models.FlowRun]:
    """
    Reads a flow run by id.

    Args:
        session: A database session
        flow_run_id: a flow run id

    Returns:
        orm_models.FlowRun: the flow run
    """
    select = (
        sa.select(db.FlowRun)
        .where(db.FlowRun.id == flow_run_id)
        .options(
            selectinload(db.FlowRun.work_queue).selectinload(db.WorkQueue.work_pool)
        )
    )

    if for_update:
        select = select.with_for_update()

    result = await session.execute(select)
    return result.scalar()


async def _apply_flow_run_filters(
    db: PrefectDBInterface,
    query: Select[T],
    flow_filter: Optional[schemas.filters.FlowFilter] = None,
    flow_run_filter: Optional[schemas.filters.FlowRunFilter] = None,
    task_run_filter: Optional[schemas.filters.TaskRunFilter] = None,
    deployment_filter: Optional[schemas.filters.DeploymentFilter] = None,
    work_pool_filter: Optional[schemas.filters.WorkPoolFilter] = None,
    work_queue_filter: Optional[schemas.filters.WorkQueueFilter] = None,
) -> Select[T]:
    """
    Applies filters to a flow run query as a combination of EXISTS subqueries.
    """

    if flow_run_filter:
        query = query.where(flow_run_filter.as_sql_filter())

    if deployment_filter:
        deployment_exists_clause = select(db.Deployment).where(
            db.Deployment.id == db.FlowRun.deployment_id,
            deployment_filter.as_sql_filter(),
        )
        query = query.where(deployment_exists_clause.exists())

    if work_pool_filter:
        work_pool_exists_clause = select(db.WorkPool).where(
            db.WorkQueue.id == db.FlowRun.work_queue_id,
            db.WorkPool.id == db.WorkQueue.work_pool_id,
            work_pool_filter.as_sql_filter(),
        )

        query = query.where(work_pool_exists_clause.exists())

    if work_queue_filter:
        work_queue_exists_clause = select(db.WorkQueue).where(
            db.WorkQueue.id == db.FlowRun.work_queue_id,
            work_queue_filter.as_sql_filter(),
        )
        query = query.where(work_queue_exists_clause.exists())

    if flow_filter or task_run_filter:
        flow_or_task_run_exists_clause: Union[
            Select[Tuple[db.Flow]],
            Select[Tuple[db.TaskRun]],
        ]

        if flow_filter:
            flow_or_task_run_exists_clause = select(db.Flow).where(
                db.Flow.id == db.FlowRun.flow_id,
                flow_filter.as_sql_filter(),
            )

        if task_run_filter:
            if not flow_filter:
                flow_or_task_run_exists_clause = select(db.TaskRun).where(
                    db.TaskRun.flow_run_id == db.FlowRun.id
                )
            else:
                flow_or_task_run_exists_clause = flow_or_task_run_exists_clause.join(
                    db.TaskRun,
                    db.TaskRun.flow_run_id == db.FlowRun.id,
                )
            flow_or_task_run_exists_clause = flow_or_task_run_exists_clause.where(
                db.FlowRun.id == db.TaskRun.flow_run_id,
                task_run_filter.as_sql_filter(),
            )

        query = query.where(flow_or_task_run_exists_clause.exists())

    return query


@db_injector
async def read_flow_runs(
    db: PrefectDBInterface,
    session: AsyncSession,
    columns: Optional[list[str]] = None,
    flow_filter: Optional[schemas.filters.FlowFilter] = None,
    flow_run_filter: Optional[schemas.filters.FlowRunFilter] = None,
    task_run_filter: Optional[schemas.filters.TaskRunFilter] = None,
    deployment_filter: Optional[schemas.filters.DeploymentFilter] = None,
    work_pool_filter: Optional[schemas.filters.WorkPoolFilter] = None,
    work_queue_filter: Optional[schemas.filters.WorkQueueFilter] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    sort: schemas.sorting.FlowRunSort = schemas.sorting.FlowRunSort.ID_DESC,
) -> Sequence[orm_models.FlowRun]:
    """
    Read flow runs.

    Args:
        session: a database session
        columns: a list of the flow run ORM columns to load, for performance
        flow_filter: only select flow runs whose flows match these filters
        flow_run_filter: only select flow runs match these filters
        task_run_filter: only select flow runs whose task runs match these filters
        deployment_filter: only select flow runs whose deployments match these filters
        offset: Query offset
        limit: Query limit
        sort: Query sort

    Returns:
        List[orm_models.FlowRun]: flow runs
    """
    query = (
        select(db.FlowRun)
        .order_by(*sort.as_sql_sort())
        .options(
            selectinload(db.FlowRun.work_queue).selectinload(db.WorkQueue.work_pool)
        )
    )

    if columns:
        query = query.options(load_only(*columns))

    query = await _apply_flow_run_filters(
        db,
        query,
        flow_filter=flow_filter,
        flow_run_filter=flow_run_filter,
        task_run_filter=task_run_filter,
        deployment_filter=deployment_filter,
        work_pool_filter=work_pool_filter,
        work_queue_filter=work_queue_filter,
    )

    if offset is not None:
        query = query.offset(offset)

    if limit is not None:
        query = query.limit(limit)

    result = await session.execute(query)
    return result.scalars().unique().all()


async def cleanup_flow_run_concurrency_slots(
    session: AsyncSession,
    flow_run: orm_models.FlowRun,
) -> None:
    """
    Cleanup flow run related resources, such as releasing concurrency slots.
    All operations should be idempotent and safe to call multiple times.
    IMPORTANT: This run may no longer exist in the database when this operation occurs.
    """

    if (
        flow_run.deployment_id
        and flow_run.state
        and flow_run.state.type
        in (
            schemas.states.StateType.PENDING,
            schemas.states.StateType.RUNNING,
            schemas.states.StateType.CANCELLING,
        )
    ):
        deployment = await models.deployments.read_deployment(
            session, flow_run.deployment_id
        )
        if deployment and deployment.concurrency_limit_id:
            await models.concurrency_limits_v2.bulk_decrement_active_slots(
                session, [deployment.concurrency_limit_id], 1
            )


class DependencyResult(PrefectBaseModel):
    id: UUID
    name: str
    upstream_dependencies: List[TaskRunResult]
    state: Optional[State]
    expected_start_time: Optional[datetime.datetime]
    start_time: Optional[datetime.datetime]
    end_time: Optional[datetime.datetime]
    total_run_time: Optional[datetime.timedelta]
    estimated_run_time: Optional[datetime.timedelta]
    untrackable_result: bool


async def read_task_run_dependencies(
    session: AsyncSession,
    flow_run_id: UUID,
) -> List[DependencyResult]:
    """
    Get a task run dependency map for a given flow run.
    """
    flow_run = await models.flow_runs.read_flow_run(
        session=session, flow_run_id=flow_run_id
    )
    if not flow_run:
        raise ObjectNotFoundError(f"Flow run with id {flow_run_id} not found")

    task_runs = await models.task_runs.read_task_runs(
        session=session,
        flow_run_filter=schemas.filters.FlowRunFilter(
            id=schemas.filters.FlowRunFilterId(any_=[flow_run_id])
        ),
    )

    dependency_graph = []

    for task_run in task_runs:
        inputs = list(set(chain(*task_run.task_inputs.values())))
        untrackable_result_status = (
            False
            if task_run.state is None
            else task_run.state.state_details.untrackable_result
        )
        dependency_graph.append(
            DependencyResult(
                id=task_run.id,
                upstream_dependencies=inputs,
                state=task_run.state,
                expected_start_time=task_run.expected_start_time,
                name=task_run.name,
                start_time=task_run.start_time,
                end_time=task_run.end_time,
                total_run_time=task_run.total_run_time,
                estimated_run_time=task_run.estimated_run_time,
                untrackable_result=untrackable_result_status,
            )
        )

    return dependency_graph


@db_injector
async def count_flow_runs(
    db: PrefectDBInterface,
    session: AsyncSession,
    flow_filter: Optional[schemas.filters.FlowFilter] = None,
    flow_run_filter: Optional[schemas.filters.FlowRunFilter] = None,
    task_run_filter: Optional[schemas.filters.TaskRunFilter] = None,
    deployment_filter: Optional[schemas.filters.DeploymentFilter] = None,
    work_pool_filter: Optional[schemas.filters.WorkPoolFilter] = None,
    work_queue_filter: Optional[schemas.filters.WorkQueueFilter] = None,
) -> int:
    """
    Count flow runs.

    Args:
        session: a database session
        flow_filter: only count flow runs whose flows match these filters
        flow_run_filter: only count flow runs that match these filters
        task_run_filter: only count flow runs whose task runs match these filters
        deployment_filter: only count flow runs whose deployments match these filters

    Returns:
        int: count of flow runs
    """

    query = select(sa.func.count(None)).select_from(db.FlowRun)

    query = await _apply_flow_run_filters(
        db,
        query,
        flow_filter=flow_filter,
        flow_run_filter=flow_run_filter,
        task_run_filter=task_run_filter,
        deployment_filter=deployment_filter,
        work_pool_filter=work_pool_filter,
        work_queue_filter=work_queue_filter,
    )

    result = await session.execute(query)
    return result.scalar_one()


@db_injector
async def delete_flow_run(
    db: PrefectDBInterface, session: AsyncSession, flow_run_id: UUID
) -> bool:
    """
    Delete a flow run by flow_run_id, handling concurrency limits if applicable.

    Args:
        session: A database session
        flow_run_id: a flow run id

    Returns:
        bool: whether or not the flow run was deleted
    """
    flow_run = await read_flow_run(session, flow_run_id)
    if not flow_run:
        return False

    deployment_id = flow_run.deployment_id

    if deployment_id:
        await cleanup_flow_run_concurrency_slots(session=session, flow_run=flow_run)

    # Delete the flow run
    result = await session.execute(
        delete(db.FlowRun).where(db.FlowRun.id == flow_run_id)
    )

    return result.rowcount > 0


async def set_flow_run_state(
    session: AsyncSession,
    flow_run_id: UUID,
    state: schemas.states.State,
    force: bool = False,
    flow_policy: Optional[Type[FlowRunOrchestrationPolicy]] = None,
    orchestration_parameters: Optional[Dict[str, Any]] = None,
    client_version: Optional[str] = None,
) -> OrchestrationResult:
    """
    Creates a new orchestrated flow run state.

    Setting a new state on a run is the one of the principal actions that is governed by
    Prefect's orchestration logic. Setting a new run state will not guarantee creation,
    but instead trigger orchestration rules to govern the proposed `state` input. If
    the state is considered valid, it will be written to the database. Otherwise, a
    it's possible a different state, or no state, will be created. A `force` flag is
    supplied to bypass a subset of orchestration logic.

    Args:
        session: a database session
        flow_run_id: the flow run id
        state: a flow run state model
        force: if False, orchestration rules will be applied that may alter or prevent
            the state transition. If True, orchestration rules are not applied.

    Returns:
        OrchestrationResult object
    """

    # load the flow run
    run = await models.flow_runs.read_flow_run(
        session=session,
        flow_run_id=flow_run_id,
        # Lock the row to prevent orchestration race conditions
        for_update=True,
    )

    if not run:
        raise ObjectNotFoundError(f"Flow run with id {flow_run_id} not found")

    initial_state = run.state.as_state() if run.state else None
    initial_state_type = initial_state.type if initial_state else None
    proposed_state_type = state.type if state else None
    intended_transition = (initial_state_type, proposed_state_type)

    if force or flow_policy is None:
        flow_policy = MinimalFlowPolicy

    orchestration_rules = flow_policy.compile_transition_rules(*intended_transition)  # type: ignore
    global_rules = GlobalFlowPolicy.compile_transition_rules(*intended_transition)

    context = FlowOrchestrationContext(
        session=session,
        run=run,
        initial_state=initial_state,
        proposed_state=state,
        client_version=client_version,
    )

    if orchestration_parameters is not None:
        context.parameters = orchestration_parameters

    # apply orchestration rules and create the new flow run state
    async with contextlib.AsyncExitStack() as stack:
        for rule in orchestration_rules:
            context = await stack.enter_async_context(
                rule(context, *intended_transition)
            )

        for rule in global_rules:
            context = await stack.enter_async_context(
                rule(context, *intended_transition)
            )

        await context.validate_proposed_state()

    if context.orchestration_error is not None:
        raise context.orchestration_error

    result = OrchestrationResult(
        state=context.validated_state,
        status=context.response_status,
        details=context.response_details,
    )

    return result


@db_injector
async def read_flow_run_graph(
    db: PrefectDBInterface,
    session: AsyncSession,
    flow_run_id: UUID,
    since: datetime.datetime = earliest_possible_datetime(),
) -> Graph:
    """Given a flow run, return the graph of it's task and subflow runs. If a `since`
    datetime is provided, only return items that may have changed since that time."""
    if isinstance(since, str):
        since = DateTime.fromisoformat(since)

    return await db.queries.flow_run_graph_v2(
        session=session,
        flow_run_id=flow_run_id,
        since=since,
        max_nodes=PREFECT_API_MAX_FLOW_RUN_GRAPH_NODES.value(),
        max_artifacts=PREFECT_API_MAX_FLOW_RUN_GRAPH_ARTIFACTS.value(),
    )


async def with_system_labels_for_flow_run(
    session: AsyncSession,
    flow_run: Union[schemas.core.FlowRun, schemas.actions.FlowRunCreate],
) -> schemas.core.KeyValueLabels:
    """Augment user supplied labels with system default labels for a flow
    run."""

    user_supplied_labels = flow_run.labels or {}

    # `deployment_id` is deprecated on `schemas.actions.FlowRunCreate`. Only
    # check `deployment_id` if given an instance of a `schemas.core.FlowRun`.
    if isinstance(flow_run, schemas.core.FlowRun) and flow_run.deployment_id:
        deployment = await models.deployments.read_deployment(
            session, deployment_id=flow_run.deployment_id
        )
        if deployment:
            # Use the deployment flow run utility for consistent label generation
            return await models.deployments.with_system_labels_for_deployment_flow_run(
                session=session,
                deployment=deployment,
                user_supplied_labels=user_supplied_labels,
            )

    # If the flow run is not part of a deployment, generate basic flow labels
    default_labels = cast(
        schemas.core.KeyValueLabels,
        {
            "prefect.flow.id": str(flow_run.flow_id),
        },
    )

    parent_labels = await models.flows.read_flow_labels(session, flow_run.flow_id) or {}

    return parent_labels | default_labels | user_supplied_labels


@db_injector
async def update_flow_run_labels(
    db: PrefectDBInterface,
    session: AsyncSession,
    flow_run_id: UUID,
    labels: KeyValueLabels,
) -> bool:
    """
    Update flow run labels by patching existing labels with new values.
    Args:
        session: A database session
        flow_run_id: the flow run id to update
        labels: the new labels to patch into existing labels
    Returns:
        bool: whether the update was successful
    """
    # First read the existing flow run to get current labels
    flow_run: Optional[orm_models.FlowRun] = await read_flow_run(session, flow_run_id)
    if not flow_run:
        raise ObjectNotFoundError(f"Flow run with id {flow_run_id} not found")

    # Merge existing labels with new labels
    current_labels = flow_run.labels or {}
    updated_labels = {**current_labels, **labels}

    try:
        # Update the flow run with merged labels
        result = await session.execute(
            sa.update(db.FlowRun)
            .where(db.FlowRun.id == flow_run_id)
            .values(labels=updated_labels)
        )
        success = result.rowcount > 0
        if success:
            await session.commit()  # Explicitly commit
        return success
    except Exception:
        raise

from __future__ import annotations

import abc
import asyncio
import copy
import inspect
import logging
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from functools import partial
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    ClassVar,
    Generator,
    NoReturn,
    Optional,
    Type,
    Union,
)

import anyio.to_thread
from pydantic import Field, PrivateAttr
from typing_extensions import Self

from prefect.context import ContextModel
from prefect.exceptions import (
    ConfigurationError,
    MissingContextError,
    SerializationError,
)
from prefect.filesystems import NullFileSystem
from prefect.logging.loggers import LoggingAdapter, get_logger, get_run_logger
from prefect.results import (
    ResultRecord,
    ResultStore,
    get_result_store,
)
from prefect.utilities._engine import get_hook_name
from prefect.utilities.annotations import NotSet
from prefect.utilities.asyncutils import run_coro_as_sync
from prefect.utilities.collections import AutoEnum

logger: logging.Logger = get_logger("transactions")


class IsolationLevel(AutoEnum):
    READ_COMMITTED = AutoEnum.auto()
    SERIALIZABLE = AutoEnum.auto()


class CommitMode(AutoEnum):
    EAGER = AutoEnum.auto()
    LAZY = AutoEnum.auto()
    OFF = AutoEnum.auto()


class TransactionState(AutoEnum):
    PENDING = AutoEnum.auto()
    ACTIVE = AutoEnum.auto()
    STAGED = AutoEnum.auto()
    COMMITTED = AutoEnum.auto()
    ROLLED_BACK = AutoEnum.auto()


class BaseTransaction(ContextModel, abc.ABC):
    """
    A base model for transaction state.
    """

    store: Optional[ResultStore] = None
    key: Optional[str] = None
    children: list[Self] = Field(default_factory=list)
    commit_mode: Optional[CommitMode] = None
    isolation_level: Optional[IsolationLevel] = IsolationLevel.READ_COMMITTED
    state: TransactionState = TransactionState.PENDING
    on_commit_hooks: list[Callable[[Self], None]] = Field(default_factory=list)
    on_rollback_hooks: list[Callable[[Self], None]] = Field(default_factory=list)
    overwrite: bool = False
    logger: Union[logging.Logger, LoggingAdapter] = Field(
        default_factory=partial(get_logger, "transactions")
    )
    write_on_commit: bool = True
    _stored_values: dict[str, Any] = PrivateAttr(default_factory=dict)
    _staged_value: ResultRecord[Any] | Any = None
    _holder: str = PrivateAttr(default_factory=lambda: str(uuid.uuid4()))
    __var__: ClassVar[ContextVar[Self]] = ContextVar("transaction")

    def set(self, name: str, value: Any) -> None:
        """
        Set a stored value in the transaction.

        Args:
            name: The name of the value to set
            value: The value to set

        Examples:
            Set a value for use later in the transaction:
            ```python
            with transaction() as txn:
                txn.set("key", "value")
                ...
                assert txn.get("key") == "value"
            ```
        """
        self._stored_values[name] = value

    def get(self, name: str, default: Any = NotSet) -> Any:
        """
        Get a stored value from the transaction.

        Child transactions will return values from their parents unless a value with
        the same name is set in the child transaction.

        Direct changes to returned values will not update the stored value. To update the
        stored value, use the `set` method.

        Args:
            name: The name of the value to get
            default: The default value to return if the value is not found

        Returns:
            The value from the transaction

        Examples:
            Get a value from the transaction:
            ```python
            with transaction() as txn:
                txn.set("key", "value")
                ...
                assert txn.get("key") == "value"
            ```

            Get a value from a parent transaction:
            ```python
            with transaction() as parent:
                parent.set("key", "parent_value")
                with transaction() as child:
                    assert child.get("key") == "parent_value"
            ```

            Update a stored value:
            ```python
            with transaction() as txn:
                txn.set("key", [1, 2, 3])
                value = txn.get("key")
                value.append(4)
                # Stored value is not updated until `.set` is called
                assert value == [1, 2, 3, 4]
                assert txn.get("key") == [1, 2, 3]

                txn.set("key", value)
                assert txn.get("key") == [1, 2, 3, 4]
            ```
        """
        # deepcopy to prevent mutation of stored values
        value = copy.deepcopy(self._stored_values.get(name, NotSet))
        if value is NotSet:
            # if there's a parent transaction, get the value from the parent
            parent = self.get_parent()
            if parent is not None:
                value = parent.get(name, default)
            # if there's no parent transaction, use the default
            elif default is not NotSet:
                value = default
            else:
                raise ValueError(f"Could not retrieve value for unknown key: {name}")
        return value

    def is_committed(self) -> bool:
        return self.state == TransactionState.COMMITTED

    def is_rolled_back(self) -> bool:
        return self.state == TransactionState.ROLLED_BACK

    def is_staged(self) -> bool:
        return self.state == TransactionState.STAGED

    def is_pending(self) -> bool:
        return self.state == TransactionState.PENDING

    def is_active(self) -> bool:
        return self.state == TransactionState.ACTIVE

    def prepare_transaction(self) -> None:
        """Helper method to prepare transaction state and validate configuration."""
        if self._token is not None:
            raise RuntimeError(
                "Context already entered. Context enter calls cannot be nested."
            )
        parent = get_transaction()
        # set default commit behavior; either inherit from parent or set a default of eager
        if self.commit_mode is None:
            self.commit_mode = parent.commit_mode if parent else CommitMode.LAZY
        # set default isolation level; either inherit from parent or set a default of read committed
        if self.isolation_level is None:
            self.isolation_level = (
                parent.isolation_level if parent else IsolationLevel.READ_COMMITTED
            )

        assert self.isolation_level is not None, "Isolation level was not set correctly"
        if (
            self.store
            and self.key
            and not self.store.supports_isolation_level(self.isolation_level)
        ):
            raise ConfigurationError(
                f"Isolation level {self.isolation_level.name} is not supported by provided "
                "configuration. Please ensure you've provided a lock file directory or lock "
                "manager when using the SERIALIZABLE isolation level."
            )

        # this needs to go before begin, which could set the state to committed
        self.state = TransactionState.ACTIVE

    def add_child(self, transaction: Self) -> None:
        self.children.append(transaction)

    def get_parent(self) -> Self | None:
        parent = None
        if self._token:
            prev_var = self._token.old_value
            if prev_var != Token.MISSING:
                parent = prev_var
        else:
            # `_token` has been reset so we need to get the active transaction from the context var
            parent = self.get_active()
        return parent

    def stage(
        self,
        value: Any,
        on_rollback_hooks: Optional[list[Callable[..., Any]]] = None,
        on_commit_hooks: Optional[list[Callable[..., Any]]] = None,
    ) -> None:
        """
        Stage a value to be committed later.
        """
        on_commit_hooks = on_commit_hooks or []
        on_rollback_hooks = on_rollback_hooks or []

        if self.state != TransactionState.COMMITTED:
            self._staged_value = value
            self.on_rollback_hooks += on_rollback_hooks
            self.on_commit_hooks += on_commit_hooks
            self.state = TransactionState.STAGED

    @classmethod
    def get_active(cls: Type[Self]) -> Optional[Self]:
        return cls.__var__.get(None)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, BaseTransaction):
            return False
        return dict(self) == dict(other)


class Transaction(BaseTransaction):
    """
    A model representing the state of a transaction.
    """

    def __enter__(self) -> Self:
        self.prepare_transaction()
        self.begin()
        self._token = self.__var__.set(self)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        exc_type, exc_val, _ = exc_info
        if not self._token:
            raise RuntimeError(
                "Asymmetric use of context. Context exit called without an enter."
            )
        if exc_type:
            self.rollback()
            self.reset()
            raise exc_val

        if self.commit_mode == CommitMode.EAGER:
            self.commit()

        # if parent, let them take responsibility
        if self.get_parent():
            self.reset()
            return

        if self.commit_mode == CommitMode.OFF:
            # if no one took responsibility to commit, rolling back
            # note that rollback returns if already committed
            self.rollback()
        elif self.commit_mode == CommitMode.LAZY:
            # no one left to take responsibility for committing
            self.commit()

        self.reset()

    def begin(self) -> None:
        if (
            self.store
            and self.key
            and self.isolation_level == IsolationLevel.SERIALIZABLE
        ):
            self.logger.debug(f"Acquiring lock for transaction {self.key!r}")
            self.store.acquire_lock(self.key, holder=self._holder)
        if (
            not self.overwrite
            and self.store
            and self.key
            and self.store.exists(key=self.key)
        ):
            self.state = TransactionState.COMMITTED

    def read(self) -> ResultRecord[Any] | None:
        if self.store and self.key:
            return self.store.read(key=self.key, holder=self._holder)
        return None

    def reset(self) -> None:
        parent = self.get_parent()

        if parent:
            # parent takes responsibility
            parent.add_child(self)

        if self._token:
            self.__var__.reset(self._token)
            self._token = None

        # do this below reset so that get_transaction() returns the relevant txn
        if parent and self.state == TransactionState.ROLLED_BACK:
            parent.rollback()

    def commit(self) -> bool:
        if self.state in [TransactionState.ROLLED_BACK, TransactionState.COMMITTED]:
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key, holder=self._holder)

            return False

        try:
            for child in self.children:
                if inspect.iscoroutinefunction(child.commit):
                    run_coro_as_sync(child.commit())
                else:
                    child.commit()

            for hook in self.on_commit_hooks:
                self.run_hook(hook, "commit")

            if self.store and self.key and self.write_on_commit:
                if isinstance(self._staged_value, ResultRecord):
                    self.store.persist_result_record(
                        result_record=self._staged_value, holder=self._holder
                    )
                else:
                    self.store.write(
                        key=self.key, obj=self._staged_value, holder=self._holder
                    )

            self.state = TransactionState.COMMITTED
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key, holder=self._holder)
            return True
        except SerializationError as exc:
            if self.logger:
                self.logger.warning(
                    f"Encountered an error while serializing result for transaction {self.key!r}: {exc}"
                    " Code execution will continue, but the transaction will not be committed.",
                )
            self.rollback()
            return False
        except Exception:
            if self.logger:
                self.logger.exception(
                    f"An error was encountered while committing transaction {self.key!r}",
                    exc_info=True,
                )
            self.rollback()
            return False

    def run_hook(self, hook: Callable[..., Any], hook_type: str) -> None:
        hook_name = get_hook_name(hook)
        # Undocumented way to disable logging for a hook. Subject to change.
        should_log = getattr(hook, "log_on_run", True)

        if should_log:
            self.logger.info(f"Running {hook_type} hook {hook_name!r}")

        try:
            if asyncio.iscoroutinefunction(hook):
                run_coro_as_sync(hook(self))
            else:
                hook(self)
        except Exception as exc:
            if should_log:
                self.logger.error(
                    f"An error was encountered while running {hook_type} hook {hook_name!r}",
                )
            raise exc
        else:
            if should_log:
                self.logger.info(
                    f"{hook_type.capitalize()} hook {hook_name!r} finished running successfully"
                )

    def rollback(self) -> bool:
        if self.state in [TransactionState.ROLLED_BACK, TransactionState.COMMITTED]:
            return False

        try:
            for hook in reversed(self.on_rollback_hooks):
                self.run_hook(hook, "rollback")

            self.state: TransactionState = TransactionState.ROLLED_BACK

            for child in reversed(self.children):
                if inspect.iscoroutinefunction(child.rollback):
                    run_coro_as_sync(child.rollback())
                else:
                    child.rollback()

            return True
        except Exception:
            if self.logger:
                self.logger.exception(
                    f"An error was encountered while rolling back transaction {self.key!r}",
                    exc_info=True,
                )
            return False
        finally:
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key, holder=self._holder)


class AsyncTransaction(BaseTransaction):
    """
    A model representing the state of an asynchronous transaction.
    """

    async def begin(self) -> None:
        if (
            self.store
            and self.key
            and self.isolation_level == IsolationLevel.SERIALIZABLE
        ):
            self.logger.debug(f"Acquiring lock for transaction {self.key!r}")
            await self.store.aacquire_lock(self.key, holder=self._holder)
        if (
            not self.overwrite
            and self.store
            and self.key
            and await self.store.aexists(key=self.key)
        ):
            self.state = TransactionState.COMMITTED

    async def read(self) -> ResultRecord[Any] | None:
        if self.store and self.key:
            return await self.store.aread(key=self.key, holder=self._holder)
        return None

    async def reset(self) -> None:
        parent = self.get_parent()

        if parent:
            # parent takes responsibility
            parent.add_child(self)

        if self._token:
            self.__var__.reset(self._token)
            self._token = None

        # do this below reset so that get_transaction() returns the relevant txn
        if parent and self.state == TransactionState.ROLLED_BACK:
            await parent.rollback()

    async def commit(self) -> bool:
        if self.state in [TransactionState.ROLLED_BACK, TransactionState.COMMITTED]:
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key, holder=self._holder)

            return False

        try:
            for child in self.children:
                if isinstance(child, AsyncTransaction):
                    await child.commit()
                else:
                    child.commit()

            for hook in self.on_commit_hooks:
                await self.run_hook(hook, "commit")

            if self.store and self.key and self.write_on_commit:
                if isinstance(self._staged_value, ResultRecord):
                    await self.store.apersist_result_record(
                        result_record=self._staged_value, holder=self._holder
                    )
                else:
                    await self.store.awrite(
                        key=self.key, obj=self._staged_value, holder=self._holder
                    )

            self.state = TransactionState.COMMITTED
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key, holder=self._holder)
            return True
        except SerializationError as exc:
            if self.logger:
                self.logger.warning(
                    f"Encountered an error while serializing result for transaction {self.key!r}: {exc}"
                    " Code execution will continue, but the transaction will not be committed.",
                )
            await self.rollback()
            return False
        except Exception:
            if self.logger:
                self.logger.exception(
                    f"An error was encountered while committing transaction {self.key!r}",
                    exc_info=True,
                )
            await self.rollback()
            return False

    async def run_hook(self, hook: Callable[..., Any], hook_type: str) -> None:
        hook_name = get_hook_name(hook)
        # Undocumented way to disable logging for a hook. Subject to change.
        should_log = getattr(hook, "log_on_run", True)

        if should_log:
            self.logger.info(f"Running {hook_type} hook {hook_name!r}")

        try:
            if asyncio.iscoroutinefunction(hook):
                await hook(self)
            else:
                await anyio.to_thread.run_sync(hook, self)
        except Exception as exc:
            if should_log:
                self.logger.error(
                    f"An error was encountered while running {hook_type} hook {hook_name!r}",
                )
            raise exc
        else:
            if should_log:
                self.logger.info(
                    f"{hook_type.capitalize()} hook {hook_name!r} finished running successfully"
                )

    async def rollback(self) -> bool:
        if self.state in [TransactionState.ROLLED_BACK, TransactionState.COMMITTED]:
            return False

        try:
            for hook in reversed(self.on_rollback_hooks):
                await self.run_hook(hook, "rollback")

            self.state: TransactionState = TransactionState.ROLLED_BACK

            for child in reversed(self.children):
                if isinstance(child, AsyncTransaction):
                    await child.rollback()
                else:
                    child.rollback()

            return True
        except Exception:
            if self.logger:
                self.logger.exception(
                    f"An error was encountered while rolling back transaction {self.key!r}",
                    exc_info=True,
                )
            return False
        finally:
            if (
                self.store
                and self.key
                and self.isolation_level == IsolationLevel.SERIALIZABLE
            ):
                self.logger.debug(f"Releasing lock for transaction {self.key!r}")
                self.store.release_lock(self.key, holder=self._holder)

    async def __aenter__(self) -> Self:
        self.prepare_transaction()
        await self.begin()
        self._token = self.__var__.set(self)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        exc_type, exc_val, _ = exc_info
        if not self._token:
            raise RuntimeError(
                "Asymmetric use of context. Context exit called without an enter."
            )
        if exc_type:
            await self.rollback()
            await self.reset()
            raise exc_val

        if self.commit_mode == CommitMode.EAGER:
            await self.commit()

        # if parent, let them take responsibility
        if self.get_parent():
            await self.reset()
            return

        if self.commit_mode == CommitMode.OFF:
            # if no one took responsibility to commit, rolling back
            # note that rollback returns if already committed
            await self.rollback()
        elif self.commit_mode == CommitMode.LAZY:
            # no one left to take responsibility for committing
            await self.commit()

        await self.reset()

    def __enter__(self) -> NoReturn:
        raise NotImplementedError(
            "AsyncTransaction does not support the `with` statement. Use the `async with` statement instead."
        )

    def __exit__(self, *exc_info: Any) -> NoReturn:
        raise NotImplementedError(
            "AsyncTransaction does not support the `with` statement. Use the `async with` statement instead."
        )


def get_transaction() -> BaseTransaction | None:
    return BaseTransaction.get_active()


@contextmanager
def transaction(
    key: str | None = None,
    store: ResultStore | None = None,
    commit_mode: CommitMode | None = None,
    isolation_level: IsolationLevel | None = None,
    overwrite: bool = False,
    write_on_commit: bool = True,
    logger: logging.Logger | LoggingAdapter | None = None,
) -> Generator[Transaction, None, None]:
    """
    A context manager for opening and managing a transaction.

    Args:
        - key: An identifier to use for the transaction
        - store: The store to use for persisting the transaction result. If not provided,
            a default store will be used based on the current run context.
        - commit_mode: The commit mode controlling when the transaction and
            child transactions are committed
        - overwrite: Whether to overwrite an existing transaction record in the store
        - write_on_commit: Whether to write the result to the store on commit. If not provided,
            will default will be determined by the current run context. If no run context is
            available, the value of `PREFECT_RESULTS_PERSIST_BY_DEFAULT` will be used.

    Yields:
        - Transaction: An object representing the transaction state
    """
    # if there is no key, we won't persist a record
    if key and not store:
        store = get_result_store()

    # Avoid inheriting a NullFileSystem for metadata_storage from a flow's result store
    if store and isinstance(store.metadata_storage, NullFileSystem):
        store = store.model_copy(update={"metadata_storage": None})

    try:
        _logger: Union[logging.Logger, LoggingAdapter] = logger or get_run_logger()
    except MissingContextError:
        _logger = get_logger("transactions")

    with Transaction(
        key=key,
        store=store,
        commit_mode=commit_mode,
        isolation_level=isolation_level,
        overwrite=overwrite,
        write_on_commit=write_on_commit,
        logger=_logger,
    ) as txn:
        yield txn


@asynccontextmanager
async def atransaction(
    key: str | None = None,
    store: ResultStore | None = None,
    commit_mode: CommitMode | None = None,
    isolation_level: IsolationLevel | None = None,
    overwrite: bool = False,
    write_on_commit: bool = True,
    logger: logging.Logger | LoggingAdapter | None = None,
) -> AsyncGenerator[AsyncTransaction, None]:
    """
    An asynchronous context manager for opening and managing an asynchronous transaction.

    Args:
        - key: An identifier to use for the transaction
        - store: The store to use for persisting the transaction result. If not provided,
            a default store will be used based on the current run context.
        - commit_mode: The commit mode controlling when the transaction and
            child transactions are committed
        - overwrite: Whether to overwrite an existing transaction record in the store
        - write_on_commit: Whether to write the result to the store on commit. If not provided,
            the default will be determined by the current run context. If no run context is
            available, the value of `PREFECT_RESULTS_PERSIST_BY_DEFAULT` will be used.

    Yields:
        - AsyncTransaction: An object representing the transaction state
    """

    # if there is no key, we won't persist a record
    if key and not store:
        store = get_result_store()

    # Avoid inheriting a NullFileSystem for metadata_storage from a flow's result store
    if store and isinstance(store.metadata_storage, NullFileSystem):
        store = store.model_copy(update={"metadata_storage": None})

    try:
        _logger: Union[logging.Logger, LoggingAdapter] = logger or get_run_logger()
    except MissingContextError:
        _logger = get_logger("transactions")

    async with AsyncTransaction(
        key=key,
        store=store,
        commit_mode=commit_mode,
        isolation_level=isolation_level,
        overwrite=overwrite,
        write_on_commit=write_on_commit,
        logger=_logger,
    ) as txn:
        yield txn

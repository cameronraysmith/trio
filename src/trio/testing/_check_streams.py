# Generic stream tests
from __future__ import annotations

import random
import sys
from contextlib import contextmanager, suppress
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Generator,
    Generic,
    Tuple,
    TypeVar,
)

from .. import CancelScope, _core
from .._abc import AsyncResource, HalfCloseableStream, ReceiveStream, SendStream, Stream
from .._highlevel_generic import aclose_forcefully
from ._checkpoints import assert_checkpoints

if TYPE_CHECKING:
    from types import TracebackType

    from typing_extensions import ParamSpec, TypeAlias

    ArgsT = ParamSpec("ArgsT")

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup

Res1 = TypeVar("Res1", bound=AsyncResource)
Res2 = TypeVar("Res2", bound=AsyncResource)
StreamMaker: TypeAlias = Callable[[], Awaitable[Tuple[Res1, Res2]]]


class _ForceCloseBoth(Generic[Res1, Res2]):
    def __init__(self, both: tuple[Res1, Res2]) -> None:
        self._first, self._second = both

    async def __aenter__(self) -> tuple[Res1, Res2]:
        return self._first, self._second

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await aclose_forcefully(self._first)
        finally:
            await aclose_forcefully(self._second)


# This is used in this file instead of pytest.raises in order to avoid a dependency
# on pytest, as the check_* functions are publicly exported.
@contextmanager
def _assert_raises(
    expected_exc: type[BaseException],
    wrapped: bool = False,
) -> Generator[None, None, None]:
    __tracebackhide__ = True
    try:
        yield
    except BaseExceptionGroup as exc:
        assert wrapped, "caught exceptiongroup, but expected an unwrapped exception"
        # assert in except block ignored below
        assert len(exc.exceptions) == 1  # noqa: PT017
        assert isinstance(exc.exceptions[0], expected_exc)  # noqa: PT017
    except expected_exc:
        assert not wrapped, "caught exception, but expected an exceptiongroup"
    else:
        raise AssertionError(f"expected exception: {expected_exc}")


async def check_one_way_stream(
    stream_maker: StreamMaker[SendStream, ReceiveStream],
    clogged_stream_maker: StreamMaker[SendStream, ReceiveStream] | None,
) -> None:
    """Perform a number of generic tests on a custom one-way stream
    implementation.

    Args:
      stream_maker: An async (!) function which returns a connected
          (:class:`~trio.abc.SendStream`, :class:`~trio.abc.ReceiveStream`)
          pair.
      clogged_stream_maker: Either None, or an async function similar to
          stream_maker, but with the extra property that the returned stream
          is in a state where ``send_all`` and
          ``wait_send_all_might_not_block`` will block until ``receive_some``
          has been called. This allows for more thorough testing of some edge
          cases, especially around ``wait_send_all_might_not_block``.

    Raises:
      AssertionError: if a test fails.

    """
    async with _ForceCloseBoth(await stream_maker()) as (s, r):
        assert isinstance(s, SendStream)
        assert isinstance(r, ReceiveStream)

        async def do_send_all(data: bytes | bytearray | memoryview) -> None:
            with assert_checkpoints():  # We're testing that it doesn't return anything.
                assert await s.send_all(data) is None  # type: ignore[func-returns-value]

        async def do_receive_some(max_bytes: int | None = None) -> bytes | bytearray:
            with assert_checkpoints():
                return await r.receive_some(max_bytes)

        async def checked_receive_1(expected: bytes) -> None:
            assert await do_receive_some(1) == expected

        async def do_aclose(resource: AsyncResource) -> None:
            with assert_checkpoints():
                await resource.aclose()

        # Simple sending/receiving
        async with _core.open_nursery() as nursery:
            nursery.start_soon(do_send_all, b"x")
            nursery.start_soon(checked_receive_1, b"x")

        async def send_empty_then_y() -> None:
            # Streams should tolerate sending b"" without giving it any
            # special meaning.
            await do_send_all(b"")
            await do_send_all(b"y")

        async with _core.open_nursery() as nursery:
            nursery.start_soon(send_empty_then_y)
            nursery.start_soon(checked_receive_1, b"y")

        # ---- Checking various argument types ----

        # send_all accepts bytearray and memoryview
        async with _core.open_nursery() as nursery:
            nursery.start_soon(do_send_all, bytearray(b"1"))
            nursery.start_soon(checked_receive_1, b"1")

        async with _core.open_nursery() as nursery:
            nursery.start_soon(do_send_all, memoryview(b"2"))
            nursery.start_soon(checked_receive_1, b"2")

        # max_bytes must be a positive integer
        with _assert_raises(ValueError):
            await r.receive_some(-1)
        with _assert_raises(ValueError):
            await r.receive_some(0)
        with _assert_raises(TypeError):
            await r.receive_some(1.5)  # type: ignore[arg-type]
        # it can also be missing or None
        async with _core.open_nursery() as nursery:
            nursery.start_soon(do_send_all, b"x")
            assert await do_receive_some() == b"x"
        async with _core.open_nursery() as nursery:
            nursery.start_soon(do_send_all, b"x")
            assert await do_receive_some(None) == b"x"

        with _assert_raises(_core.BusyResourceError, wrapped=True):
            async with _core.open_nursery() as nursery:
                nursery.start_soon(do_receive_some, 1)
                nursery.start_soon(do_receive_some, 1)

        # Method always has to exist, and an empty stream with a blocked
        # receive_some should *always* allow send_all. (Technically it's legal
        # for send_all to wait until receive_some is called to run, though; a
        # stream doesn't *have* to have any internal buffering. That's why we
        # start a concurrent receive_some call, then cancel it.)
        async def simple_check_wait_send_all_might_not_block(
            scope: CancelScope,
        ) -> None:
            with assert_checkpoints():
                await s.wait_send_all_might_not_block()
            scope.cancel()

        async with _core.open_nursery() as nursery:
            nursery.start_soon(
                simple_check_wait_send_all_might_not_block,
                nursery.cancel_scope,
            )
            nursery.start_soon(do_receive_some, 1)

        # closing the r side leads to BrokenResourceError on the s side
        # (eventually)
        async def expect_broken_stream_on_send() -> None:
            with _assert_raises(_core.BrokenResourceError):
                while True:
                    await do_send_all(b"x" * 100)

        async with _core.open_nursery() as nursery:
            nursery.start_soon(expect_broken_stream_on_send)
            nursery.start_soon(do_aclose, r)

        # once detected, the stream stays broken
        with _assert_raises(_core.BrokenResourceError):
            await do_send_all(b"x" * 100)

        # r closed -> ClosedResourceError on the receive side
        with _assert_raises(_core.ClosedResourceError):
            await do_receive_some(4096)

        # we can close the same stream repeatedly, it's fine
        await do_aclose(r)
        await do_aclose(r)

        # closing the sender side
        await do_aclose(s)

        # now trying to send raises ClosedResourceError
        with _assert_raises(_core.ClosedResourceError):
            await do_send_all(b"x" * 100)

        # even if it's an empty send
        with _assert_raises(_core.ClosedResourceError):
            await do_send_all(b"")

        # ditto for wait_send_all_might_not_block
        with _assert_raises(_core.ClosedResourceError):
            with assert_checkpoints():
                await s.wait_send_all_might_not_block()

        # and again, repeated closing is fine
        await do_aclose(s)
        await do_aclose(s)

    async with _ForceCloseBoth(await stream_maker()) as (s, r):
        # if send-then-graceful-close, receiver gets data then b""
        async def send_then_close() -> None:
            await do_send_all(b"y")
            await do_aclose(s)

        async def receive_send_then_close() -> None:
            # We want to make sure that if the sender closes the stream before
            # we read anything, then we still get all the data. But some
            # streams might block on the do_send_all call. So we let the
            # sender get as far as it can, then we receive.
            await _core.wait_all_tasks_blocked()
            await checked_receive_1(b"y")
            await checked_receive_1(b"")
            await do_aclose(r)

        async with _core.open_nursery() as nursery:
            nursery.start_soon(send_then_close)
            nursery.start_soon(receive_send_then_close)

    async with _ForceCloseBoth(await stream_maker()) as (s, r):
        await aclose_forcefully(r)

        with _assert_raises(_core.BrokenResourceError):
            while True:
                await do_send_all(b"x" * 100)

        with _assert_raises(_core.ClosedResourceError):
            await do_receive_some(4096)

    async with _ForceCloseBoth(await stream_maker()) as (s, r):
        await aclose_forcefully(s)

        with _assert_raises(_core.ClosedResourceError):
            await do_send_all(b"123")

        # after the sender does a forceful close, the receiver might either
        # get BrokenResourceError or a clean b""; either is OK. Not OK would be
        # if it freezes, or returns data.
        with suppress(_core.BrokenResourceError):
            await checked_receive_1(b"")

    # cancelled aclose still closes
    async with _ForceCloseBoth(await stream_maker()) as (s, r):
        with _core.CancelScope() as scope:
            scope.cancel()
            await r.aclose()

        with _core.CancelScope() as scope:
            scope.cancel()
            await s.aclose()

        with _assert_raises(_core.ClosedResourceError):
            await do_send_all(b"123")

        with _assert_raises(_core.ClosedResourceError):
            await do_receive_some(4096)

    # Check that we can still gracefully close a stream after an operation has
    # been cancelled. This can be challenging if cancellation can leave the
    # stream internals in an inconsistent state, e.g. for
    # SSLStream. Unfortunately this test isn't very thorough; the really
    # challenging case for something like SSLStream is it gets cancelled
    # *while* it's sending data on the underlying, not before. But testing
    # that requires some special-case handling of the particular stream setup;
    # we can't do it here. Maybe we could do a bit better with
    #     https://github.com/python-trio/trio/issues/77
    async with _ForceCloseBoth(await stream_maker()) as (s, r):

        async def expect_cancelled(
            afn: Callable[ArgsT, Awaitable[object]],
            *args: ArgsT.args,
            **kwargs: ArgsT.kwargs,
        ) -> None:
            with _assert_raises(_core.Cancelled):
                await afn(*args, **kwargs)

        with _core.CancelScope() as scope:
            scope.cancel()
            async with _core.open_nursery() as nursery:
                nursery.start_soon(expect_cancelled, do_send_all, b"x")
                nursery.start_soon(expect_cancelled, do_receive_some, 1)

        async with _core.open_nursery() as nursery:
            nursery.start_soon(do_aclose, s)
            nursery.start_soon(do_aclose, r)

    # Check that if a task is blocked in receive_some, then closing the
    # receive stream causes it to wake up.
    async with _ForceCloseBoth(await stream_maker()) as (s, r):

        async def receive_expecting_closed():
            with _assert_raises(_core.ClosedResourceError):
                await r.receive_some(10)

        async with _core.open_nursery() as nursery:
            nursery.start_soon(receive_expecting_closed)
            await _core.wait_all_tasks_blocked()
            await aclose_forcefully(r)

    # check wait_send_all_might_not_block, if we can
    if clogged_stream_maker is not None:
        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):
            record: list[str] = []

            async def waiter(cancel_scope: CancelScope) -> None:
                record.append("waiter sleeping")
                with assert_checkpoints():
                    await s.wait_send_all_might_not_block()
                record.append("waiter wokeup")
                cancel_scope.cancel()

            async def receiver() -> None:
                # give wait_send_all_might_not_block a chance to block
                await _core.wait_all_tasks_blocked()
                record.append("receiver starting")
                while True:
                    await r.receive_some(16834)

            async with _core.open_nursery() as nursery:
                nursery.start_soon(waiter, nursery.cancel_scope)
                await _core.wait_all_tasks_blocked()
                nursery.start_soon(receiver)

            assert record == [
                "waiter sleeping",
                "receiver starting",
                "waiter wokeup",
            ]

        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):
            # simultaneous wait_send_all_might_not_block fails
            with _assert_raises(_core.BusyResourceError, wrapped=True):
                async with _core.open_nursery() as nursery:
                    nursery.start_soon(s.wait_send_all_might_not_block)
                    nursery.start_soon(s.wait_send_all_might_not_block)

            # and simultaneous send_all and wait_send_all_might_not_block (NB
            # this test might destroy the stream b/c we end up cancelling
            # send_all and e.g. SSLStream can't handle that, so we have to
            # recreate afterwards)
            with _assert_raises(_core.BusyResourceError, wrapped=True):
                async with _core.open_nursery() as nursery:
                    nursery.start_soon(s.wait_send_all_might_not_block)
                    nursery.start_soon(s.send_all, b"123")

        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):
            # send_all and send_all blocked simultaneously should also raise
            # (but again this might destroy the stream)
            with _assert_raises(_core.BusyResourceError, wrapped=True):
                async with _core.open_nursery() as nursery:
                    nursery.start_soon(s.send_all, b"123")
                    nursery.start_soon(s.send_all, b"123")

        # closing the receiver causes wait_send_all_might_not_block to return,
        # with or without an exception
        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):

            async def sender() -> None:
                try:
                    with assert_checkpoints():
                        await s.wait_send_all_might_not_block()
                except _core.BrokenResourceError:  # pragma: no cover
                    pass

            async def receiver() -> None:
                await _core.wait_all_tasks_blocked()
                await aclose_forcefully(r)

            async with _core.open_nursery() as nursery:
                nursery.start_soon(sender)
                nursery.start_soon(receiver)

        # and again with the call starting after the close
        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):
            await aclose_forcefully(r)
            try:
                with assert_checkpoints():
                    await s.wait_send_all_might_not_block()
            except _core.BrokenResourceError:  # pragma: no cover
                pass

        # Check that if a task is blocked in a send-side method, then closing
        # the send stream causes it to wake up.
        async def close_soon(s: SendStream) -> None:
            await _core.wait_all_tasks_blocked()
            await aclose_forcefully(s)

        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):
            async with _core.open_nursery() as nursery:
                nursery.start_soon(close_soon, s)
                with _assert_raises(_core.ClosedResourceError):
                    await s.send_all(b"xyzzy")

        async with _ForceCloseBoth(await clogged_stream_maker()) as (s, r):
            async with _core.open_nursery() as nursery:
                nursery.start_soon(close_soon, s)
                with _assert_raises(_core.ClosedResourceError):
                    await s.wait_send_all_might_not_block()


async def check_two_way_stream(
    stream_maker: StreamMaker[Stream, Stream],
    clogged_stream_maker: StreamMaker[Stream, Stream] | None,
) -> None:
    """Perform a number of generic tests on a custom two-way stream
    implementation.

    This is similar to :func:`check_one_way_stream`, except that the maker
    functions are expected to return objects implementing the
    :class:`~trio.abc.Stream` interface.

    This function tests a *superset* of what :func:`check_one_way_stream`
    checks – if you call this, then you don't need to also call
    :func:`check_one_way_stream`.

    """
    await check_one_way_stream(stream_maker, clogged_stream_maker)

    async def flipped_stream_maker() -> tuple[Stream, Stream]:
        return (await stream_maker())[::-1]

    flipped_clogged_stream_maker: Callable[[], Awaitable[tuple[Stream, Stream]]] | None

    if clogged_stream_maker is not None:

        async def flipped_clogged_stream_maker() -> tuple[Stream, Stream]:
            return (await clogged_stream_maker())[::-1]

    else:
        flipped_clogged_stream_maker = None
    await check_one_way_stream(flipped_stream_maker, flipped_clogged_stream_maker)

    async with _ForceCloseBoth(await stream_maker()) as (s1, s2):
        assert isinstance(s1, Stream)
        assert isinstance(s2, Stream)

        # Duplex can be a bit tricky, might as well check it as well
        DUPLEX_TEST_SIZE = 2**20
        CHUNK_SIZE_MAX = 2**14

        r = random.Random(0)
        i = r.getrandbits(8 * DUPLEX_TEST_SIZE)
        test_data = i.to_bytes(DUPLEX_TEST_SIZE, "little")

        async def sender(
            s: Stream,
            data: bytes | bytearray | memoryview,
            seed: int,
        ) -> None:
            r = random.Random(seed)
            m = memoryview(data)
            while m:
                chunk_size = r.randint(1, CHUNK_SIZE_MAX)
                await s.send_all(m[:chunk_size])
                m = m[chunk_size:]

        async def receiver(s: Stream, data: bytes | bytearray, seed: int) -> None:
            r = random.Random(seed)
            got = bytearray()
            while len(got) < len(data):
                chunk = await s.receive_some(r.randint(1, CHUNK_SIZE_MAX))
                assert chunk
                got += chunk
            assert got == data

        async with _core.open_nursery() as nursery:
            nursery.start_soon(sender, s1, test_data, 0)
            nursery.start_soon(sender, s2, test_data[::-1], 1)
            nursery.start_soon(receiver, s1, test_data[::-1], 2)
            nursery.start_soon(receiver, s2, test_data, 3)

        async def expect_receive_some_empty() -> None:
            assert await s2.receive_some(10) == b""
            await s2.aclose()

        async with _core.open_nursery() as nursery:
            nursery.start_soon(expect_receive_some_empty)
            nursery.start_soon(s1.aclose)


async def check_half_closeable_stream(
    stream_maker: StreamMaker[HalfCloseableStream, HalfCloseableStream],
    clogged_stream_maker: StreamMaker[HalfCloseableStream, HalfCloseableStream] | None,
) -> None:
    """Perform a number of generic tests on a custom half-closeable stream
    implementation.

    This is similar to :func:`check_two_way_stream`, except that the maker
    functions are expected to return objects that implement the
    :class:`~trio.abc.HalfCloseableStream` interface.

    This function tests a *superset* of what :func:`check_two_way_stream`
    checks – if you call this, then you don't need to also call
    :func:`check_two_way_stream`.

    """
    await check_two_way_stream(stream_maker, clogged_stream_maker)

    async with _ForceCloseBoth(await stream_maker()) as (s1, s2):
        assert isinstance(s1, HalfCloseableStream)
        assert isinstance(s2, HalfCloseableStream)

        async def send_x_then_eof(s: HalfCloseableStream) -> None:
            await s.send_all(b"x")
            with assert_checkpoints():
                await s.send_eof()

        async def expect_x_then_eof(r: HalfCloseableStream) -> None:
            await _core.wait_all_tasks_blocked()
            assert await r.receive_some(10) == b"x"
            assert await r.receive_some(10) == b""

        async with _core.open_nursery() as nursery:
            nursery.start_soon(send_x_then_eof, s1)
            nursery.start_soon(expect_x_then_eof, s2)

        # now sending is disallowed
        with _assert_raises(_core.ClosedResourceError):
            await s1.send_all(b"y")

        # but we can do send_eof again
        with assert_checkpoints():
            await s1.send_eof()

        # and we can still send stuff back the other way
        async with _core.open_nursery() as nursery:
            nursery.start_soon(send_x_then_eof, s2)
            nursery.start_soon(expect_x_then_eof, s1)

    if clogged_stream_maker is not None:
        async with _ForceCloseBoth(await clogged_stream_maker()) as (s1, s2):
            # send_all and send_eof simultaneously is not ok
            with _assert_raises(_core.BusyResourceError, wrapped=True):
                async with _core.open_nursery() as nursery:
                    nursery.start_soon(s1.send_all, b"x")
                    await _core.wait_all_tasks_blocked()
                    nursery.start_soon(s1.send_eof)

        async with _ForceCloseBoth(await clogged_stream_maker()) as (s1, s2):
            # wait_send_all_might_not_block and send_eof simultaneously is not
            # ok either
            with _assert_raises(_core.BusyResourceError, wrapped=True):
                async with _core.open_nursery() as nursery:
                    nursery.start_soon(s1.wait_send_all_might_not_block)
                    await _core.wait_all_tasks_blocked()
                    nursery.start_soon(s1.send_eof)

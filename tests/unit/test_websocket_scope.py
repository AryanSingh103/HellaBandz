"""WebSocket validation is independent from HTTP scope middleware."""
import asyncio

from starlette.datastructures import QueryParams

from hellabandz.api.app import stream


def test_stream_rejects_duplicate_portfolio_parameters_before_accept():
    class Socket:
        query_params = QueryParams("portfolio_id=a&portfolio_id=main")
        accepted = False
        closed = None

        async def accept(self):
            self.accepted = True

        async def close(self, **kwargs):
            self.closed = kwargs

    socket = Socket()
    asyncio.run(stream(socket))
    assert not socket.accepted
    assert socket.closed["code"] == 1008

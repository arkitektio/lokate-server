"""Running a GraphQL document as one of the suite's callers (the ``run`` fixture)."""

from typing import Any, Literal, overload

from graphql import GraphQLError
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse
from strawberry.types import ExecutionResult

from lokate_server.schema import schema


def context(token: str) -> HttpContext:
    return HttpContext(
        request=UniversalRequest(_extensions={}),
        response=TemporalResponse(),
        headers={"Authorization": f"Bearer {token}"},
        type="http",
    )


def first_error(result: ExecutionResult) -> GraphQLError:
    """The first error of a result that was expected to fail."""
    assert result.errors, "the document did not fail"
    return result.errors[0]


def code(result: ExecutionResult) -> object:
    """The ``code`` extension of a failed result's first error."""
    extensions = first_error(result).extensions
    assert extensions is not None
    return extensions["code"]


class Run:
    """Run a document as ``token`` (phone-a by default).

    Fails the test on errors and answers the data, unless ``errors=True``: then it answers
    the whole result.
    """

    @overload
    async def __call__(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        token: str = "phone-a",
        errors: Literal[False] = False,
    ) -> dict[str, Any]: ...

    @overload
    async def __call__(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        token: str = "phone-a",
        *,
        errors: Literal[True],
    ) -> ExecutionResult: ...

    async def __call__(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        token: str = "phone-a",
        errors: bool = False,
    ) -> dict[str, Any] | ExecutionResult:
        result = await schema.execute(
            document, variable_values=variables or {}, context_value=context(token)
        )
        if not errors:
            assert not result.errors, result.errors
            assert result.data is not None
            return result.data
        return result

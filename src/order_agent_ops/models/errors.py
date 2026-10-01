"""Project-level model errors independent of provider SDKs."""


class ModelGatewayError(RuntimeError):
    """Base error returned by the model gateway."""


class ModelTimeoutError(ModelGatewayError):
    """The configured model call timeout was exceeded."""


class ProviderInvocationError(ModelGatewayError):
    """A provider raised an implementation-specific error."""


class InvalidModelResponseError(ModelGatewayError):
    """The provider returned data that is not a JSON object."""


class ModelResponseValidationError(ModelGatewayError):
    """The JSON object did not satisfy the requested Pydantic schema."""

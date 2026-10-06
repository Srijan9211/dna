"""Errors shared by every tracker's authentication client."""


class AuthBackendUnavailable(ValueError):
    """The tracker could not be reached or failed to answer.

    Transient: nothing is known about the credentials, so the caller answers 503
    ("try again shortly") rather than 401 ("wrong password").  It subclasses
    ``ValueError`` so code written before this existed, which treats any
    ``ValueError`` from a login as a rejection, still fails closed.
    """

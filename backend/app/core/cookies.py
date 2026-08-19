"""The session cookie's NAME and its ``Secure`` flag, decided in exactly one place.

Split out of ``api.auth`` for two reasons. The middleware that guards
state-changing requests (``core.csrf``) has to know which cookie carries a
session, and an ``api -> core`` import is the layering this project allows, not
the reverse. And the name is no longer a constant, so there must be nowhere left
to hardcode it.

Why the name is not a constant any more
---------------------------------------

The cookie now carries the ``__Host-`` prefix, which a browser enforces
structurally: it refuses to accept the cookie at all unless it is ``Secure``, has
``Path=/``, and has **no ``Domain``**. That last one is the interesting half. This
codebase already has a rule that no cookie may ever be set with a ``Domain``, and
a test asserting the attribute's absence -- but a rule and a test are things a
future change can talk its way past. ``__Host-`` moves the same guarantee into the
browser: with the prefix, a ``Domain`` attribute does not weaken host isolation,
it makes the cookie *not exist*. A subdomain -- or anything that has taken one
over -- also cannot overwrite a ``__Host-`` cookie, which closes the session-
fixation half of the same problem that ``Domain``-less alone does not.

The constraint that shapes this module is that the prefix REQUIRES ``Secure``, and
:func:`cookie_secure` deliberately returns ``False`` for local development,
because local is served over plain HTTP on localhost where a ``Secure`` cookie is
simply never sent -- a prefixed cookie there would not harden anything, it would
break login on a developer's machine.

So the prefix has to be conditional on precisely the same predicate as ``Secure``,
and the two must be decided by one function or they will drift into a
half-prefixed cookie that no browser accepts. Hence :func:`session_cookie_name`,
and hence the fact that both it and :func:`cookie_secure` read the same
``environment != "local"`` test one line apart.

The trade-off, stated plainly
-----------------------------

**The cookie's name now depends on the environment**, which is a genuine cost:

* a reader that hardcodes ``"sma_session"`` works locally and silently fails to
  find the session in staging and production -- the worst possible failure shape,
  because it passes every test on a laptop. The mitigation is that this function
  is the only way to obtain the name; ``api.auth`` exports no name constant, and
  the test suite resolves it through here too rather than repeating the literal;
* promoting an environment changes the name, so a browser holding the old cookie
  appears logged out. It is not *accepted* -- reading both names in production
  would hand back exactly the fixation weakness the prefix was added to remove, so
  the un-prefixed name is never read outside local. One re-login is the whole cost,
  and it only happens on the transition.

The alternatives were considered and rejected:

* **one unconditional ``__Host-`` name.** The name stops being environment-
  dependent, and local login stops working -- the browser drops the cookie because
  the response is not over TLS. Making local HTTPS to fix a cookie name is a worse
  trade than a resolver function.
* **no prefix at all**, keeping the existing ``Domain``-less cookie plus its test.
  That is what was already here. It leaves the guarantee enforced by convention on
  our side of the wire, where the prefix would have the browser enforce it, and it
  leaves subdomain overwrite open.
"""

from typing import Final

from backend.app.core.config import Settings

#: The cookie name before any prefix. Never used directly as a cookie name --
#: :func:`session_cookie_name` decides whether it is used bare or prefixed.
SESSION_COOKIE_BASE_NAME: Final = "sma_session"

#: RFC 6265bis section 4.1.3. A browser accepts a cookie whose name starts with
#: this ONLY if it is ``Secure``, its ``Path`` is ``/``, and it carries no
#: ``Domain``. Nothing here needs to enforce those; the browser refuses the
#: ``Set-Cookie`` outright otherwise, which is the point of using it.
HOST_COOKIE_PREFIX: Final = "__Host-"


def cookie_secure(settings: Settings) -> bool:
    """``Secure`` everywhere except local development.

    Local is served over plain HTTP on localhost, where a ``Secure`` cookie is
    never sent -- so the flag would not harden anything, it would break login.
    Every other environment terminates TLS.

    This is also the predicate :func:`session_cookie_name` keys off, and that is
    not a coincidence: ``__Host-`` requires ``Secure``, so any environment where
    the two disagree produces a cookie the browser throws away.
    """
    return settings.environment != "local"


def session_cookie_name(settings: Settings) -> str:
    """The name of the session cookie in this environment.

    ``__Host-sma_session`` wherever the cookie is ``Secure``; plain
    ``sma_session`` in local development, where it cannot be. See the module
    docstring for why the two are tied together and what that costs.
    """
    if cookie_secure(settings):
        return f"{HOST_COOKIE_PREFIX}{SESSION_COOKIE_BASE_NAME}"
    return SESSION_COOKIE_BASE_NAME

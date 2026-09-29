"""Identity, roles and row-level scope.

Scope lands here in the foundation rather than in a late phase, because *which
entities a principal may see* is a schema concern: every repository read takes a
:class:`~mrip.auth.scope.Scope`, so it cannot be forgotten at a call site.

The split across this package mirrors the questions being answered:

- :mod:`mrip.auth.scope` — which rows? (row-level, from ``user_scopes``)
- :mod:`mrip.auth.principal` — who, and what may they do? (role, a rank)
- :mod:`mrip.auth.passwords` — is this credential genuine? (Argon2id)
- :mod:`mrip.auth.tokens` — is this session genuine? (signed, short-lived)
- :mod:`mrip.auth.service` — the login transaction that ties them together

``service`` is not re-exported here: it imports the store, and a cycle through
``mrip.auth`` would make the leaf modules unimportable from the repository layer.
"""

from mrip.auth.principal import AccessDeniedError, Principal
from mrip.auth.scope import SCOPE_ALL, Scope

__all__ = ["SCOPE_ALL", "AccessDeniedError", "Principal", "Scope"]

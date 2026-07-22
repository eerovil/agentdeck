# Record delegation lineage before the parent is visible

AgentDeck records the delegating parent's provider source identity when delegation starts and
resolves it lazily against Presentation-Eligible Sessions, with recorded lineage taking precedence
over provider evidence discovered later. This permits cross-provider delegation and survives the
race where the parent has not yet been scanned, at the cost of temporarily unresolved lineage and
the need to distinguish provider source identity from AgentDeck-wide Session Identity.

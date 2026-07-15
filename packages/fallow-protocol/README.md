# fallow-protocol

Versioned Pydantic wire models and interface contracts shared by Fallow components. This is the
portability boundary and intentionally depends only on Pydantic and the Python standard library.

`UnitTransition` records a committed work-unit state change with its unit, job,
agent, attempt, state, and UTC time. Coordinator lifecycle logs map this wire
type to the analysis fields in `units.jsonl`.

Fallow is pre-alpha. See the [repository README](https://github.com/Unluckyathecking/fallow#readme),
[stability policy](https://github.com/Unluckyathecking/fallow/blob/main/docs/api-stability.md)
and [license](https://github.com/Unluckyathecking/fallow/blob/main/LICENSE).

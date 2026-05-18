"""Context Compiler & Assembly module.

Provides two tiers of context construction for agent queries:

**Compiler** (P5-04)
    Searches knowledge chunks and memory entries, ranks results,
    applies token budgets, and writes ``context_packs``.

**Assembly Engine** (P8-01)
    Card-based injection strategy model — resolves agent card stores,
    applies pluggable injection strategies (always / moderate / on_demand),
    and assembles the final prompt-ready text.

**Strategies** (pluggable)
    Each injection strategy is a class implementing ``IInjectionStrategy``.
    New strategies can be registered via ``@register_strategy()`` without
    modifying the engine core.

**Pipeline**
    The assembly process is decomposed into discrete ``PipelineStep``
    implementations orchestrated by ``PipelineOrchestrator``.

Public API
----------
* ``compile_context`` — compile a context pack for a given query
* ``assemble_context`` — assemble prompt-ready text from card stores
* ``IContextCompiler`` — abstract interface for swappable compilers
* ``_sensitivity_allowed`` — sensitivity ceiling check helper
* ``_content_hash`` — SHA-256 content digest helper
"""

from mneme.context.compiler import (
    compile_context,
    _sensitivity_allowed,
    _content_hash,
)
from mneme.context.interfaces import IContextCompiler
from mneme.context.assembly_engine import (
    assemble_context,
    _strategy_for,
)

__all__ = [
    "compile_context",
    "assemble_context",
    "IContextCompiler",
    "_sensitivity_allowed",
    "_content_hash",
    "_strategy_for",
]

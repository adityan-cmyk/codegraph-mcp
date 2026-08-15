# ADR-001: MCP Server Architecture

## Status
Accepted

## Context
External AI agents (opencode) need read-only access to the code dependency graph and semantic search. The server must be stateless (no session management) to survive restarts and support concurrent agents.

## Decision
Use a custom Starlette ASGI app implementing the MCP JSON-RPC protocol over HTTP. No FastMCP, no session IDs. Each request is self-contained.

## Consequences
- Agents can connect/reconnect without session state loss
- No dependency on FastMCP's session lifecycle
- Simpler deployment — no WebSocket or SSE stream management
- Must implement JSON-RPC protocol handling manually

# ADR-002: Zero-Downtime Graph Rebuild

## Status
Accepted

## Context
Neo4j graph rebuilds take minutes. During rebuild, agents must continue querying without interruption.

## Decision
Use generation tagging (gen=N) in Neo4j. New graph builds as gen N+1 while gen N serves queries. Atomic swap via GraphIndexProxy. Old generation kept for rollback.

## Consequences
- Zero query failures during rebuild (tested: 206 concurrent queries, 0 failures)
- Old graph data available for instant rollback
- Slightly higher Neo4j storage during rebuild (two generations)

# ADR-003: Shadow Collection Rebuild for Weaviate

## Status
Accepted

## Context
Semantic index rebuild requires re-embedding all chunks (minutes to hours). Queries must not return empty results during rebuild.

## Decision
Build into a shadow collection (CodeChunkB) while the active collection (CodeChunk) serves queries. Atomic swap on commit. Old collection kept for rollback.

## Consequences
- Queries always return results from the active collection
- Old data available for rollback if new build quality is worse
- Doubled Weaviate storage during rebuild

# ADR-004: Reinforcement Learning via Reranking

## Status
Accepted

## Context
Embedding model retraining is expensive and slow. We need a way to improve search quality from agent feedback without retraining.

## Decision
Apply a reranking layer on top of Weaviate results using learned per-symbol boost/penalty weights. Weights computed from feedback: boost_weight = (positive - negative) / (positive + negative + 5) * 2 - 1. Final score = embedding_score + 0.15 * boost_weight.

## Consequences
- Search quality improves with each PR review
- No model retraining needed
- Single agent cannot hijack rankings (needs multiple votes)
- At 10 accepted feedbacks, a full rebuild is auto-triggered

# ADR-005: Quality-Gated Feedback Consumption

## Status
Accepted

## Context
Not all AI agent feedback is useful. Vague or generic feedback should not influence the system.

## Decision
Each feedback entry is scored 0.0-1.0 based on: tool usage, specificity, constructiveness, expected-results description, rating consistency, and length. Only score > 0.5 is accepted.

## Consequences
- Vague feedback rejected automatically
- Only actionable feedback influences boost weights
- At 10 accepted feedbacks, auto-build triggers

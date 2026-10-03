from neo4j import GraphDatabase

from app.schemas.codebase import GraphNeighborhood


class Neo4jGraphIndex:
    def __init__(self, uri: str = "bolt://localhost:7687", username: str = "neo4j", password: str = "password", gen: int = 0) -> None:
        self._uri = uri
        self._username = username
        self._password = password
        self._gen = gen
        self._driver = None

    def _get_driver(self):
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self._uri,
                auth=(self._username, self._password),
                max_connection_pool_size=50,
                connection_acquisition_timeout=30,
            )
            self._ensure_constraints()
        return self._driver

    def _ensure_constraints(self) -> None:
        if self._driver is None:
            return
        with self._driver.session() as session:
            session.run(
                "CREATE INDEX IF NOT EXISTS FOR (s:Symbol) ON (s.gen)"
            )
            # Composite uniqueness on (id, gen): correctness for MERGE and an
            # index for every id-scoped lookup (blast radius, tests, degrees).
            session.run(
                "CREATE CONSTRAINT IF NOT EXISTS symbol_id_gen "
                "FOR (s:Symbol) REQUIRE (s.id, s.gen) IS UNIQUE"
            )
            session.run(
                "CREATE INDEX IF NOT EXISTS FOR (s:Symbol) ON (s.is_test)"
            )

    def reset(self) -> None:
        from app.core.test_guard import refuse_reset_under_pytest
        refuse_reset_under_pytest("Neo4j graph")
        with self._get_driver().session() as session:
            session.run(
                "MATCH (n:Symbol {gen: $gen}) DETACH DELETE n",
                gen=self._gen,
            )

    def cleanup_other_gens(self) -> None:
        """Delete every generation EXCEPT this backend's own. Called on the
        new graph after a swap — previous gens are also leaked by container
        restarts mid-build (deploy kills the active backend before its
        cleanup), so cleaning only the retired gen left stale nodes behind
        (gens 2/3/5 lingered with 22k nodes)."""
        with self._get_driver().session() as session:
            session.run(
                "MATCH (n:Symbol) WHERE n.gen <> $gen DETACH DELETE n",
                gen=self._gen,
            )

    def upsert_symbol(self, symbol_id: str, calls: list[str] | None = None, uses: list[str] | None = None, uses_with_modes: list[tuple[str, list[str]]] | None = None, metadata: dict | None = None) -> None:
        gen = self._gen
        with self._get_driver().session() as session:
            if metadata:
                session.run(
                    """
                    MERGE (s:Symbol {id: $symbol_id, gen: $gen})
                    SET s.gen = $gen,
                        s.kind = $kind,
                        s.file_path = $file_path,
                        s.start_line = $start_line,
                        s.end_line = $end_line,
                        s.module = $module,
                        s.is_test = $is_test
                    """,
                    symbol_id=symbol_id,
                    gen=gen,
                    kind=metadata.get("kind", ""),
                    file_path=metadata.get("file_path", ""),
                    start_line=metadata.get("start_line", 0),
                    end_line=metadata.get("end_line", 0),
                    module=metadata.get("module", ""),
                    is_test=metadata.get("is_test", False),
                )
            else:
                session.run(
                    """
                    MERGE (s:Symbol {id: $symbol_id, gen: $gen})
                    SET s.gen = $gen
                    """,
                    symbol_id=symbol_id,
                    gen=gen,
                )

            session.run(
                """
                MATCH (s:Symbol {id: $symbol_id, gen: $gen})
                OPTIONAL MATCH (s)-[r:CALLS|USES]->()
                DELETE r
                """,
                symbol_id=symbol_id,
                gen=gen,
            )

            for target in calls or []:
                session.run(
                    """
                    MERGE (target:Symbol {id: $target_id, gen: $gen})
                    SET target.gen = $gen
                    """,
                    target_id=target,
                    gen=gen,
                )
                session.run(
                    """
                    MATCH (s:Symbol {id: $symbol_id, gen: $gen})
                    MATCH (t:Symbol {id: $target_id, gen: $gen})
                    MERGE (s)-[:CALLS]->(t)
                    """,
                    symbol_id=symbol_id,
                    target_id=target,
                    gen=gen,
                )

            if uses_with_modes:
                for target, modes in uses_with_modes:
                    session.run(
                        """
                        MERGE (target:Symbol {id: $target_id, gen: $gen})
                        SET target.gen = $gen
                        """,
                        target_id=target,
                        gen=gen,
                    )
                    session.run(
                        """
                        MATCH (s:Symbol {id: $symbol_id, gen: $gen})
                        MATCH (t:Symbol {id: $target_id, gen: $gen})
                        MERGE (s)-[r:USES]->(t)
                        SET r.usage_modes = $modes
                        """,
                        symbol_id=symbol_id,
                        target_id=target,
                        gen=gen,
                        modes=modes,
                    )
            else:
                for target in uses or []:
                    session.run(
                        """
                        MERGE (target:Symbol {id: $target_id, gen: $gen})
                        SET target.gen = $gen
                        """,
                        target_id=target,
                        gen=gen,
                    )
                    session.run(
                        """
                        MATCH (s:Symbol {id: $symbol_id, gen: $gen})
                        MATCH (t:Symbol {id: $target_id, gen: $gen})
                        MERGE (s)-[:USES]->(t)
                        """,
                        symbol_id=symbol_id,
                        target_id=target,
                        gen=gen,
                    )

    def get_neighbors(self, symbol_id: str, depth: int = 1) -> GraphNeighborhood:
        gen = self._gen
        with self._get_driver().session() as session:
            upstream_result = session.run(
                f"""
                MATCH (s:Symbol {{id: $symbol_id, gen: $gen}})<-[:CALLS*1..{depth}]-(upstream:Symbol)
                WHERE upstream.gen = $gen
                RETURN DISTINCT upstream.id AS id
                """,
                symbol_id=symbol_id,
                gen=gen,
            )
            upstream = [record["id"] for record in upstream_result]

            downstream_result = session.run(
                f"""
                MATCH (s:Symbol {{id: $symbol_id, gen: $gen}})-[:CALLS*1..{depth}]->(downstream:Symbol)
                WHERE downstream.gen = $gen
                RETURN DISTINCT downstream.id AS id
                """,
                symbol_id=symbol_id,
                gen=gen,
            )
            downstream = [record["id"] for record in downstream_result]

            used_by_result = session.run(
                f"""
                MATCH (s:Symbol {{id: $symbol_id, gen: $gen}})<-[r:USES*1..{depth}]-(user:Symbol)
                WHERE user.gen = $gen
                RETURN user.id AS id, r[0].usage_modes AS modes
                """,
                symbol_id=symbol_id,
                gen=gen,
            )
            used_by_records = list(used_by_result)
            used_by = [record["id"] for record in used_by_records]
            _fallback_modes = ["reference"]
            used_by_modes = {record["id"]: (record["modes"] or _fallback_modes) for record in used_by_records}

            uses_result = session.run(
                f"""
                MATCH (s:Symbol {{id: $symbol_id, gen: $gen}})-[r:USES*1..{depth}]->(used:Symbol)
                WHERE used.gen = $gen
                RETURN used.id AS id, r[0].usage_modes AS modes
                """,
                symbol_id=symbol_id,
                gen=gen,
            )
            uses_records = list(uses_result)
            uses = [record["id"] for record in uses_records]
            uses_modes = {record["id"]: (record["modes"] or _fallback_modes) for record in uses_records}

        return GraphNeighborhood(
            symbol_id=symbol_id,
            upstream=upstream,
            downstream=downstream,
            used_by=used_by,
            uses=uses,
            used_by_modes=used_by_modes,
            uses_modes=uses_modes,
        )

    def get_blast_radius(self, symbol_id: str) -> GraphNeighborhood:
        return self.get_neighbors(symbol_id, depth=2)

    def traverse(self, symbol_id: str, depth: int = 2, max_neighborhoods: int = 100) -> list[GraphNeighborhood]:
        neighborhoods: list[GraphNeighborhood] = []
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(symbol_id, 0)]

        while queue and len(neighborhoods) < max_neighborhoods:
            current, current_depth = queue.pop(0)
            if current in visited:
                continue

            visited.add(current)
            # Always 1-hop neighborhoods: querying the seed at full `depth`
            # flattened multi-hop neighbors into hop 1, collapsing the level
            # structure (traverse reported depth-1 hops systematically).
            neighborhood = self.get_neighbors(current, depth=1)
            neighborhoods.append(neighborhood)

            if current_depth < depth:
                for neighbor in neighborhood.upstream + neighborhood.downstream + neighborhood.used_by + neighborhood.uses:
                    if neighbor not in visited:
                        queue.append((neighbor, current_depth + 1))

        return neighborhoods

    def has_symbol(self, symbol_id: str) -> bool:
        with self._get_driver().session() as session:
            result = session.run(
                "MATCH (s:Symbol {id: $symbol_id, gen: $gen}) RETURN count(s) AS count",
                symbol_id=symbol_id,
                gen=self._gen,
            )
            return result.single()["count"] > 0

    def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, object]]:
        with self._get_driver().session() as session:
            result = session.run(
                """
                MATCH (s:Symbol {gen: $gen})
                WHERE toLower(s.id) CONTAINS toLower($search_term)
                RETURN s.id AS symbol_id,
                       [(s)-[r:CALLS]->() | r][0..1] AS has_calls,
                       [(s)-[r:USES]->() | r][0..1] AS has_uses,
                       [()-[r:CALLS]->(s) | r][0..1] AS has_callers,
                       [()-[r:USES]->(s) | r][0..1] AS has_users
                LIMIT $max_results
                """,
                search_term=query,
                gen=self._gen,
                max_results=limit,
            )
            results: list[dict[str, object]] = []
            for record in result:
                sid = record["symbol_id"]
                results.append({
                    "symbol_id": sid,
                    "short_name": sid.split("::")[-1],
                    "has_calls": len(record["has_calls"]) > 0,
                    "has_uses": len(record["has_uses"]) > 0,
                    "has_callers": len(record["has_callers"]) > 0,
                    "has_users": len(record["has_users"]) > 0,
                })
            return results

    def get_tests_for(self, symbol_id: str) -> list[dict[str, object]]:
        with self._get_driver().session() as session:
            result = session.run(
                """
                MATCH (t:Symbol)-[r]->(s:Symbol {id: $symbol_id, gen: $gen})
                WHERE t.gen = $gen AND t.is_test = true AND type(r) IN ['CALLS', 'USES']
                RETURN t.id AS id, t.file_path AS file_path,
                       t.start_line AS start_line, t.end_line AS end_line,
                       type(r) AS rel
                ORDER BY t.id
                """,
                symbol_id=symbol_id,
                gen=self._gen,
            )
            return [
                {
                    "symbol_id": row["id"],
                    "relation": "calls" if row["rel"] == "CALLS" else "type_reference",
                    "file_path": row["file_path"] or "",
                    "start_line": row["start_line"] or 0,
                    "end_line": row["end_line"] or 0,
                }
                for row in result
            ]

    def get_test_symbol_ids(self) -> set[str]:
        with self._get_driver().session() as session:
            result = session.run(
                "MATCH (t:Symbol {is_test: true}) WHERE t.gen = $gen RETURN t.id AS id",
                gen=self._gen,
            )
            return {row["id"] for row in result}

    def get_symbol_degrees(self) -> dict[str, int]:
        with self._get_driver().session() as session:
            result = session.run(
                """
                MATCH (n:Symbol {gen: $gen})
                OPTIONAL MATCH (n)-[r]-()
                RETURN n.id AS id, count(r) AS degree
                """,
                gen=self._gen,
            )
            return {row["id"]: row["degree"] for row in result}

    def get_module_edges(self) -> list[tuple[str, str]]:
        """All directed (source_module -> target_module) pairs from CALLS+USES
        edges, deduped — the input for cycle detection."""
        with self._get_driver().session() as session:
            result = session.run(
                """
                MATCH (a:Symbol {gen: $gen})-[r:CALLS|USES]->(b:Symbol {gen: $gen})
                RETURN a.id AS src, b.id AS dst
                """,
                gen=self._gen,
            )
            pairs: set[tuple[str, str]] = set()
            for row in result:
                src_mod = "::".join(row["src"].split("::")[:-1])
                dst_mod = "::".join(row["dst"].split("::")[:-1])
                if src_mod and dst_mod and src_mod != dst_mod:
                    pairs.add((src_mod, dst_mod))
            return sorted(pairs)

    def get_test_caller_counts(self) -> dict[str, int]:
        with self._get_driver().session() as session:
            result = session.run(
                """
                MATCH (t:Symbol {is_test: true, gen: $gen})-[:CALLS]->(x:Symbol {gen: $gen})
                RETURN x.id AS id, count(t) AS n
                """,
                gen=self._gen,
            )
            return {row["id"]: row["n"] for row in result}

    def get_stats(self) -> dict[str, int]:
        with self._get_driver().session() as session:
            node_result = session.run(
                "MATCH (n:Symbol {gen: $gen}) RETURN count(n) AS count",
                gen=self._gen,
            )
            node_count = node_result.single()["count"]

            edge_result = session.run(
                """
                MATCH (s:Symbol {gen: $gen})-[r:CALLS|USES]->(t:Symbol {gen: $gen})
                RETURN count(r) AS count
                """,
                gen=self._gen,
            )
            edge_count = edge_result.single()["count"]

        return {
            "graph_nodes": node_count,
            "graph_edges": edge_count,
        }

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

import networkx as nx

from cascade.domain.models import World


def build_graph(world: World) -> nx.DiGraph:
    graph = nx.DiGraph()
    graph.add_nodes_from(c.id for c in world.commitments)
    graph.add_edges_from((d.from_id, d.to_id) for d in world.dependencies)
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("temporal dependency graph contains a cycle")
    return graph


def descendants(world: World, commitment_id: str) -> tuple[str, ...]:
    graph = build_graph(world)
    affected = nx.descendants(graph, commitment_id)
    return tuple(node for node in nx.topological_sort(graph) if node in affected)

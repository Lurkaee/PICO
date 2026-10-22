"""Dependency graph and schema validator for workflows (Milestone P9.1)."""

import json
from collections import defaultdict, deque
from typing import Any

from pico.capabilities import CapabilityRegistry, registry as default_registry
from pico.core.errors import ValidationError
from pico.schemas.workflow import StepDefinition, WorkflowCreate

MAX_STEPS_LIMIT = 50
MAX_GRAPH_DEPTH_LIMIT = 20
MAX_INPUT_TEMPLATE_BYTES = 65536


def extract_output_references(obj: Any) -> list[tuple[str, str]]:
    """
    Recursively scans an input template dictionary for upstream step references.
    Supported reference forms:
      - {"$from_step": "<step_key>", "$path": "<path>"}
      - {"$from_step": "<step_key>", "$output_field": "<field>"}
    Returns a list of (upstream_step_key, path) tuples.
    """
    refs: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        if "$from_step" in obj:
            step_key = str(obj["$from_step"]).strip()
            path = str(obj.get("$path") or obj.get("$output_field") or "").strip()
            refs.append((step_key, path))
        for v in obj.values():
            refs.extend(extract_output_references(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.extend(extract_output_references(item))
    return refs


def validate_workflow_graph(
    workflow_data: WorkflowCreate,
    capability_registry: CapabilityRegistry | None = None,
) -> list[StepDefinition]:
    """
    Performs comprehensive pre-dispatch graph validation:
    1. Checks graph bounds (steps count, template payload size).
    2. Enforces unique step keys.
    3. Rejects self-dependencies.
    4. Validates that all dependency references exist within the plan.
    5. Detects cycles using Kahn's topological sort algorithm.
    6. Enforces maximum dependency depth limit.
    7. Verifies that all referenced capabilities exist in the registry.
    8. Validates that any output references declare the source step in their dependencies.
    9. Validates static inputs against capability Pydantic schemas.
    """
    reg = capability_registry or default_registry
    steps = workflow_data.steps

    if not steps:
        raise ValidationError("Workflow must contain at least one step.")

    if len(steps) > MAX_STEPS_LIMIT:
        raise ValidationError(
            f"Workflow step count ({len(steps)}) exceeds maximum limit of {MAX_STEPS_LIMIT}."
        )

    # 1. Step key uniqueness
    seen_keys: set[str] = set()
    step_map: dict[str, StepDefinition] = {}
    for step in steps:
        key = step.step_key.strip()
        if key in seen_keys:
            raise ValidationError(f"Duplicate step key '{key}' found in workflow definition.")
        seen_keys.add(key)
        step_map[key] = step

    # 2. Payload size check
    for step in steps:
        serialized = json.dumps(step.input_template)
        if len(serialized.encode("utf-8")) > MAX_INPUT_TEMPLATE_BYTES:
            raise ValidationError(
                f"Step '{step.step_key}' input template exceeds maximum byte limit of {MAX_INPUT_TEMPLATE_BYTES}."
            )

    # 3. Validate dependency existence and reject self-dependencies
    adj: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {k: 0 for k in seen_keys}
    parents: dict[str, list[str]] = defaultdict(list)

    for step in steps:
        u = step.step_key
        dep_set = set()
        for dep in step.dependencies:
            dep_clean = dep.strip()
            if dep_clean == u:
                raise ValidationError(f"Step '{u}' cannot depend on itself.")
            if dep_clean not in seen_keys:
                raise ValidationError(
                    f"Step '{u}' depends on non-existent step '{dep_clean}'."
                )
            if dep_clean in dep_set:
                raise ValidationError(
                    f"Step '{u}' contains duplicate dependency '{dep_clean}'."
                )
            dep_set.add(dep_clean)
            adj[dep_clean].append(u)
            parents[u].append(dep_clean)
            in_degree[u] += 1

    # 4. Cycle detection via Kahn's algorithm (Topological sort)
    queue = deque([k for k, deg in in_degree.items() if deg == 0])
    visited_count = 0
    topological_order: list[str] = []

    # Track depth of each node from root (in-degree 0)
    depth_map: dict[str, int] = {k: 1 for k in queue}

    while queue:
        curr = queue.popleft()
        visited_count += 1
        topological_order.append(curr)
        curr_depth = depth_map[curr]

        for neighbor in adj[curr]:
            in_degree[neighbor] -= 1
            depth_map[neighbor] = max(depth_map.get(neighbor, 1), curr_depth + 1)
            if depth_map[neighbor] > MAX_GRAPH_DEPTH_LIMIT:
                raise ValidationError(
                    f"Workflow dependency depth exceeds maximum limit of {MAX_GRAPH_DEPTH_LIMIT} at step '{neighbor}'."
                )
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited_count != len(seen_keys):
        raise ValidationError(
            "Cyclic dependency detected: workflow steps form a closed loop."
        )

    # 5. Capability Resolution & Input Verification
    for step in steps:
        cap_id = step.capability_id.strip()
        if not reg.has(cap_id):
            raise ValidationError(
                f"Step '{step.step_key}' references unknown or unregistered capability '{cap_id}'."
            )
        capability = reg.get(cap_id, version=step.capability_version)

        # 6. Output reference validation
        refs = extract_output_references(step.input_template)
        declared_deps = set(step.dependencies)
        for ref_step, ref_path in refs:
            if ref_step not in declared_deps:
                raise ValidationError(
                    f"Step '{step.step_key}' references output from '{ref_step}', but '{ref_step}' is not listed in dependencies."
                )
            if not ref_path:
                raise ValidationError(
                    f"Step '{step.step_key}' has empty output field reference for upstream step '{ref_step}'."
                )

        # 7. If step has NO dynamic output references, validate static input against capability schema now
        if not refs:
            try:
                capability.input_model.model_validate(step.input_template)
            except Exception as exc:
                raise ValidationError(
                    f"Step '{step.step_key}' static input fails schema validation for '{cap_id}': {str(exc)}"
                )

    # Return steps sorted in topological dependency order
    return [step_map[k] for k in topological_order]

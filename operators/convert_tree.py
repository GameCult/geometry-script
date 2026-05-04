import collections.abc
import keyword
import re

import bpy
import mathutils


_SOCKET_TYPE_NAMES = {
    "BOOLEAN": "Bool",
    "COLLECTION": "Collection",
    "GEOMETRY": "Geometry",
    "IMAGE": "Image",
    "INT": "Int",
    "MATERIAL": "Material",
    "MENU": "Menu",
    "OBJECT": "Object",
    "RGBA": "Color",
    "ROTATION": "Rotation",
    "STRING": "String",
    "TEXTURE": "Texture",
    "VALUE": "Float",
    "VECTOR": "Vector",
}

_PROP_DENYLIST = {
    "bl_idname",
    "bl_label",
    "bl_static_type",
    "color",
    "dimensions",
    "height",
    "hide",
    "inputs",
    "internal_links",
    "label",
    "location",
    "mute",
    "name",
    "outputs",
    "parent",
    "rna_type",
    "select",
    "show_options",
    "show_preview",
    "show_texture",
    "type",
    "use_custom_color",
    "warning_propagation",
    "width",
}


def _snake_case(value):
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_").lower()
    if not value:
        value = "value"
    if value[0].isdigit():
        value = "_" + value
    if keyword.iskeyword(value):
        value += "_"
    return value


def _node_function_name(node):
    if node.bl_idname == "GeometryNodeGroup" and getattr(node, "node_tree", None):
        return _snake_case(node.node_tree.name)
    return _snake_case(node.bl_rna.name)


def _socket_name(socket):
    return _snake_case(socket.name or socket.identifier)


def _enabled_sockets(sockets):
    return [socket for socket in sockets if socket.enabled and socket.type != "CUSTOM"]


def _convert_value(value):
    if isinstance(value, mathutils.Vector):
        return tuple(value)
    if isinstance(value, mathutils.Euler):
        return tuple(value)
    if isinstance(value, mathutils.Color):
        return tuple(value)
    if isinstance(value, bpy.types.bpy_prop_array):
        return tuple(value)
    if isinstance(value, collections.abc.Iterable) and not isinstance(value, (str, bytes)):
        try:
            return tuple(value)
        except TypeError:
            return value
    return value


def _literal(value):
    value = _convert_value(value)
    if isinstance(value, bpy.types.ID):
        return "None"
    return repr(value)


def _socket_type_annotation(socket):
    return _SOCKET_TYPE_NAMES.get(socket.type, "Type")


class _Assignment:
    def __init__(self, name, node):
        self.name = name
        self.node = node
        self.props = {}
        self.arguments = {}
        self.argument_outputs = {}
        self.argument_is_multi_input = {}

    def flattened_arguments(self):
        result = []
        for value in self.arguments.values():
            if isinstance(value, _Assignment):
                result.append(value)
            elif isinstance(value, list):
                result.extend(item for item in value if isinstance(item, _Assignment))
        return result

    def _value_expr(self, key, value, index=None):
        if isinstance(value, list):
            values = [self._value_expr(key, item, index=i) for i, item in enumerate(value)]
            if self.argument_is_multi_input.get(key, False):
                return "[" + ", ".join(values) + "]"
            if len(values) == 1:
                return values[0]
            return "(" + ", ".join(values) + ")"
        if not isinstance(value, _Assignment):
            return _literal(value)
        if value.node.type == "GROUP_INPUT":
            output_name = self.argument_outputs[key] if index is None else self.argument_outputs[key][index]
            return output_name
        output_name = self.argument_outputs[key] if index is None else self.argument_outputs[key][index]
        enabled_outputs = _enabled_sockets(value.node.outputs)
        if len(enabled_outputs) > 1:
            return f"{value.name}.{output_name}"
        return value.name

    def _argument_expr(self, key, value):
        return f"{key}={self._value_expr(key, value)}"

    def result_targets(self):
        return self.name

    def to_script(self):
        args = []
        for key, value in self.props.items():
            args.append(f"{key}={_literal(value)}")
        for key, value in self.arguments.items():
            args.append(self._argument_expr(key, value))
        return f"{self.result_targets()} = {_node_function_name(self.node)}({', '.join(args)})"


def _collect_node_props(node):
    props = {}
    parent_props = set()
    if getattr(node.bl_rna, "base", None) is not None:
        parent_props.update(prop.identifier for prop in node.bl_rna.base.bl_rna.properties)
    for prop in node.bl_rna.properties:
        if prop.identifier in parent_props or prop.identifier in _PROP_DENYLIST:
            continue
        if prop.is_readonly and prop.type not in {"ENUM", "BOOLEAN", "INT", "FLOAT", "STRING"}:
            continue
        try:
            value = getattr(node, prop.identifier)
        except Exception:
            continue
        if prop.type == "POINTER":
            continue
        if prop.type == "COLLECTION":
            continue
        props[_snake_case(prop.identifier)] = _convert_value(value)
    return props


def _collect_input_defaults(node):
    props = {}
    for socket in _enabled_sockets(node.inputs):
        if not hasattr(socket, "default_value") or socket.hide_value:
            continue
        props[_socket_name(socket)] = _convert_value(socket.default_value)
    return props


def _topological_sort(root):
    seen = set()
    order = []

    def visit(assignment):
        if assignment in seen:
            return
        seen.add(assignment)
        for dependency in assignment.flattened_arguments():
            visit(dependency)
        order.append(assignment)

    visit(root)
    return order


def _function_arguments(group_input):
    if group_input is None:
        return []
    arguments = []
    for output in _enabled_sockets(group_input.outputs):
        arguments.append(f"{_socket_name(output)}: {_socket_type_annotation(output)}")
    return arguments


def _return_entries(group_output, output_assignment):
    entries = []
    for socket in _enabled_sockets(group_output.node.inputs):
        key = _socket_name(socket)
        if key not in group_output.arguments:
            continue
        value = group_output.arguments[key]
        expr = group_output._argument_expr(key, value).split("=", 1)[1]
        entries.append(f'"{socket.name}": {expr}')
    if entries:
        return "{ " + ", ".join(entries) + " }"
    return output_assignment.name


def convert_node_tree(tree):
    """Return a best-effort Geometry Script draft for a Blender node tree."""
    assignments = []
    node_to_assignment = {}
    node_type_counts = {}
    incoming_links = {}

    for node in tree.nodes:
        prefix = _snake_case(node.type[:1] or "node")
        count = node_type_counts.get(prefix, 0) + 1
        node_type_counts[prefix] = count
        assignment = _Assignment(f"{prefix}{count}", node)
        assignment.props.update(_collect_node_props(node))
        assignments.append(assignment)
        node_to_assignment[node] = assignment

    for link in tree.links:
        incoming_links.setdefault(link.to_socket, []).append((node_to_assignment[link.from_node], _socket_name(link.from_socket)))

    for assignment in assignments:
        grouped_inputs = {}
        for socket in _enabled_sockets(assignment.node.inputs):
            key = _socket_name(socket)
            grouped_inputs.setdefault(key, []).append(socket)
        for key, sockets in grouped_inputs.items():
            values = []
            output_names = {}
            is_multi_input = any(socket.is_multi_input for socket in sockets)
            for socket in sockets:
                links = incoming_links.get(socket, [])
                if links:
                    for from_assignment, output_name in links:
                        output_names[len(values)] = output_name
                        values.append(from_assignment)
                elif hasattr(socket, "default_value") and not socket.hide_value:
                    values.append(_convert_value(socket.default_value))
            if not values:
                continue
            if len(values) == 1:
                assignment.arguments[key] = values[0]
                if output_names:
                    assignment.argument_outputs[key] = output_names[0]
            else:
                assignment.arguments[key] = values
                assignment.argument_outputs[key] = output_names
            assignment.argument_is_multi_input[key] = is_multi_input

    group_output = next((assignment for assignment in assignments if assignment.node.type == "GROUP_OUTPUT"), None)
    if group_output is None:
        raise ValueError(f"Node tree {tree.name!r} has no Group Output node")
    group_input = next((node for node in tree.nodes if node.type == "GROUP_INPUT"), None)

    sorted_assignments = [
        assignment
        for assignment in _topological_sort(group_output)
        if assignment.node.type not in {"GROUP_INPUT", "GROUP_OUTPUT"}
    ]
    body = "\n    ".join(assignment.to_script() for assignment in sorted_assignments)
    if not body:
        body = "pass"

    arguments = ", ".join(_function_arguments(group_input))
    function_name = _snake_case(tree.name)
    return_expr = _return_entries(group_output, sorted_assignments[-1] if sorted_assignments else group_output)

    return (
        "from geometry_script import *\n\n"
        f'@tree("{tree.name}")\n'
        f"def {function_name}({arguments}):\n"
        f"    {body}\n"
        f"    return {return_expr}\n"
    )


class ConvertTree(bpy.types.Operator):
    bl_idname = "geometry_script.convert_tree"
    bl_label = "Convert to Geometry Script"

    @classmethod
    def poll(cls, context):
        return bool(getattr(getattr(context, "space_data", None), "node_tree", None))

    def execute(self, context):
        script = convert_node_tree(context.space_data.node_tree)
        script_datablock = bpy.data.texts.new(context.space_data.node_tree.name + ".py")
        script_datablock.write(script)
        return {"FINISHED"}

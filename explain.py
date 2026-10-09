#!/usr/bin/env python3
# Offline `kubectl explain`: renders field docs from saved OpenAPI v3 specs instead of a cluster.
#
# Run order: get openapi-v3/ once, then run this script as often as you like. Two sources:
#   golden.sh on a VM: the server's own spec, output matches kubectl exactly (enums included)
#   ./fetch-swagger.sh [tag]: upstream GitHub files, no cluster needed, but they carry no enums
# Prerequisites: Python 3.9+, ./openapi-v3/*.json next to this script.
# Usage: ./explain.py <kind>[.field.path] [--recursive] [--api-version=group/version]
#        [--color=always]
# Changes: nothing. Writes: nothing.
#
# Limits vs kubectl: kind names only (no plurals/short names), CRDs only if in the spec.
import argparse
import json
import os
import re
import sys
from pathlib import Path

SPEC_DIR = Path(__file__).with_name("openapi-v3")
WIDTH = 76  # kubectl wraps descriptions to 80 columns minus the 4-space indent
COLOR = False  # set in main() from --color, the terminal check and NO_COLOR


def paint(s, code):
    return f"\033[{code}m{s}\033[0m" if COLOR else s


def wrap(s, lim=WIDTH):
    """Port of github.com/mitchellh/go-wordwrap WrapString, which kubectl uses."""
    out, word, space, current = [], "", "", 0
    for ch in s:
        if ch == "\n":
            if word:
                out.append(space + word)
            elif current + len(space) <= lim:
                out.append(space)
            out.append("\n")
            word, space, current = "", "", 0
        elif ch.isspace() and ch != " ":
            if not space or word:
                current += len(space) + len(word)
                out.append(space + word)
                word, space = "", ""
            space += ch
        else:
            word += ch
            if current + len(space) + len(word) > lim and len(word) < lim:
                out.append("\n")
                space, current = "", 0
    if word:
        out.append(space + word)
    elif current + len(space) <= lim:
        out.append(space)
    return "".join(out)


def indent(s, n):
    pad = " " * n
    return pad + s.replace("\n", "\n" + pad)


def version_key(version):
    """Ascending sort key: GA < beta < alpha, then higher numbers first."""
    m = re.fullmatch(r"v(\d+)(alpha|beta)?(\d*)", version)
    major, stage, minor = m.groups()
    return ({"alpha": 2, "beta": 1, None: 0}[stage], -int(major), -int(minor or 0))


class Spec:
    def __init__(self, spec_dir):
        # each file holds one group/version plus the meta types it references; names are unique
        self.defs = {}
        for f in spec_dir.glob("*.json"):
            self.defs.update(
                json.loads(f.read_text()).get("components", {}).get("schemas", {})
            )

    def deref(self, schema):
        """Follow $ref, single-element allOf and array items to the schema that holds properties."""
        if "items" in schema:
            return self.deref(schema["items"])
        if len(schema.get("allOf", [])) == 1:
            return self.deref(schema["allOf"][0])
        if "$ref" in schema:
            return self.defs[schema["$ref"].rsplit("/", 1)[1]]
        return schema

    def type_name(self, schema):
        """Port of kubectl's "typeGuess" template."""
        if "items" in schema:
            return "[]" + self.type_name(schema["items"])
        if "additionalProperties" in schema:
            return "map[string]" + self.type_name(schema["additionalProperties"])
        if len(schema.get("allOf", [])) == 1 and "properties" not in schema:
            return self.type_name(schema["allOf"][0])
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[1]
            ref_type = self.defs.get(name, {}).get("type", "object")
            return name.rsplit(".", 1)[1] if ref_type == "object" else ref_type
        return schema.get("type", "Object")

    def find_kind(self, kind, api_version):
        """Return (group, version, schema) for a kind name, like kubectl's preferred version."""
        found = []
        for schema in self.defs.values():
            gvks = schema.get("x-kubernetes-group-version-kind", [])
            # shared meta types (DeleteOptions, WatchEvent) list many GVKs; skip them
            if len(gvks) == 1 and gvks[0]["kind"].lower() == kind.lower():
                g = gvks[0]
                if api_version in (None, f"{g['group']}/{g['version']}".lstrip("/")):
                    found.append((g["group"], g["version"], g["kind"], schema))
        if not found:
            sys.exit(f'error: the server doesn\'t have a resource type "{kind}"')
        # shortcut: core group first, then most stable version, then group name; kubectl
        # uses server discovery order instead. Pass --api-version if it picks the wrong one.
        return min(found, key=lambda f: (f[0] != "", version_key(f[1]), f[0]))


def enum_values(values, limit=None):
    shown = ['""' if v == "" else str(v) for v in values[:limit]]
    return shown + ["...."] if limit and len(values) > limit else shown


def field_lines(spec, schema, level, recursive, seen):
    lines = []
    required = set(schema.get("required", []))
    for name, prop in sorted(schema.get("properties", {}).items()):
        req = paint(" -required-", "31") if name in required else ""
        type_ = paint(f"<{spec.type_name(prop)}>", "32")
        lines.append(f"{'  ' * (level + 1)}{paint(name, '1;36')}\t{type_}{req}\n")
        if prop.get("enum"):
            enum = f"enum: {', '.join(enum_values(prop['enum'], 4))}"
            lines.append(f"{'  ' * (level + 1)}{paint(enum, '33')}\n")
        if recursive:
            target = spec.deref(prop)
            if id(target) not in seen and "properties" in target:
                lines += field_lines(
                    spec, target, level + 1, recursive, seen | {id(target)}
                )
        else:
            if prop.get("description"):
                lines.append(indent(wrap(prop["description"]), 4) + "\n")
            lines.append("\n")
    return lines


def main():
    # 1. Parse arguments
    p = argparse.ArgumentParser(
        description="Offline kubectl explain backed by OpenAPI v3 specs"
    )
    p.add_argument("resource", help="kind[.field.path], e.g. deployment.spec.strategy")
    p.add_argument("--recursive", action="store_true", help="print all nested fields")
    p.add_argument(
        "--api-version", help="group/version, e.g. autoscaling/v2 (core: v1)"
    )
    p.add_argument(
        "--color",
        choices=["auto", "always"],
        default="auto",
        help="auto: terminal only, unless NO_COLOR is set; always: e.g. for less -R",
    )
    args = p.parse_args()
    # auto keeps piped output byte-identical to kubectl
    global COLOR
    COLOR = args.color == "always" or (
        sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )
    kind, *path = args.resource.split(".")

    # 2. Resolve the kind, then walk the field path
    spec = Spec(SPEC_DIR)
    group, version, kind_name, schema = spec.find_kind(kind, args.api_version)
    field = None
    for name in path:
        props = spec.deref(schema).get("properties", {})
        if name not in props:
            sys.exit(f'error: field "{name}" does not exist')
        field, schema = props[name], spec.deref(props[name])

    # 3. Render
    out = f"{paint('GROUP:', '1')}      {group}\n" if group else ""
    out += f"{paint('KIND:', '1')}       {kind_name}\n"
    out += f"{paint('VERSION:', '1')}    {version}\n\n"
    descriptions = [schema.get("description", "")]
    if field is not None:
        out += f"{paint('FIELD:', '1')} {paint(path[-1], '1;36')} "
        out += paint(f"<{spec.type_name(field)}>", "32") + "\n"
        if field.get("enum"):
            out += paint("ENUM:", "1") + "\n"
            out += "".join(
                f"    {paint(v, '33')}\n" for v in enum_values(field["enum"])
            )
        else:
            out += "\n"
        out += "\n"
        # a field that points at another type also shows that type's description
        descriptions = [field.get("description", "")] + (
            descriptions if schema is not field else []
        )
    out += (
        paint("DESCRIPTION:", "1")
        + "\n"
        + indent(wrap("\n".join(d for d in descriptions if d)) + "\n", 4)
        + "\n"
    )
    if "properties" in schema:
        out += paint("FIELDS:", "1") + "\n" + "".join(
            field_lines(spec, schema, 0, args.recursive, {id(schema)})
        )
    sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()

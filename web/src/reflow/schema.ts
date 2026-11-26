// The JSON Schema subset that the published formats are written in (schema/*.schema.json at the
// repository root), read two ways: `validate` checks a value against a schema at load, and
// `generateTypes` writes the TypeScript types of a schema (bun run schema:gen). Python validates
// the same files with jsonschema (tests/test_viewer_contract.py). Both functions reject a keyword
// outside the subset, so a schema can't say something one side would silently ignore.

export type Schema = {
  $schema?: string;
  $id?: string;
  $ref?: string;
  $defs?: Record<string, Schema>;
  title?: string;
  description?: string;
  type?: TypeName | TypeName[];
  enum?: Json[];
  const?: Json;
  minimum?: number;
  exclusiveMinimum?: number;
  pattern?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: Schema | false;
  propertyNames?: Schema;
  prefixItems?: Schema[];
  items?: Schema | false;
  minItems?: number;
};

type TypeName = "string" | "number" | "integer" | "boolean" | "null" | "array" | "object";
type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

const KEYWORDS = new Set(["$schema", "$id", "$ref", "$defs", "title", "description", "type", "enum",
  "const", "minimum", "exclusiveMinimum", "pattern", "properties", "required", "additionalProperties",
  "propertyNames", "prefixItems", "items", "minItems"]);

function checkKeywords(schema: Schema, at: string): void {
  for (const key of Object.keys(schema)) {
    if (!KEYWORDS.has(key)) throw new Error(`schema ${at || "/"}: "${key}" is outside the supported subset (web/src/reflow/schema.ts)`);
  }
}

function deref(schema: Schema, root: Schema): Schema {
  if (!schema.$ref) return schema;
  const name = /^#\/\$defs\/(.+)$/.exec(schema.$ref)?.[1];
  const target = name ? root.$defs?.[name] : undefined;
  if (!target) throw new Error(`schema: unresolved $ref ${schema.$ref}`);
  return target;
}

function typeOf(value: unknown): TypeName {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  return typeof value as TypeName;
}

const show = (value: unknown) => {
  const text = JSON.stringify(value) ?? String(value);
  return text.length > 40 ? `${text.slice(0, 37)}...` : text;
};

/** Every way `value` breaks `schema`, each as "<path>: <what>"; [] when it is valid. */
export function validate(schema: Schema, value: unknown, root: Schema = schema, path = ""): string[] {
  checkKeywords(schema, path);
  const s = deref(schema, root);
  if (s !== schema) return validate(s, value, root, path);
  const at = path || "(root)";
  const actual = typeOf(value);
  if (s.type) {
    const allowed = Array.isArray(s.type) ? s.type : [s.type];
    if (!allowed.some((t) => t === actual || (t === "number" && actual === "integer"))) {
      return [`${at}: expected ${allowed.join(" or ")}, got ${show(value)}`];
    }
  }
  if (s.const !== undefined && JSON.stringify(value) !== JSON.stringify(s.const)) {
    return [`${at}: expected ${show(s.const)}, got ${show(value)}`];
  }
  if (s.enum && !s.enum.some((e) => JSON.stringify(e) === JSON.stringify(value))) {
    return [`${at}: expected one of ${s.enum.map(show).join(", ")}, got ${show(value)}`];
  }
  const errors: string[] = [];
  if (typeof value === "number") {
    if (s.minimum !== undefined && value < s.minimum) errors.push(`${at}: ${value} is below ${s.minimum}`);
    if (s.exclusiveMinimum !== undefined && value <= s.exclusiveMinimum) errors.push(`${at}: ${value} is not above ${s.exclusiveMinimum}`);
  }
  if (typeof value === "string" && s.pattern && !new RegExp(s.pattern, "u").test(value)) {
    errors.push(`${at}: ${show(value)} does not match ${s.pattern}`);
  }
  if (Array.isArray(value)) {
    if (s.minItems !== undefined && value.length < s.minItems) errors.push(`${at}: has ${value.length} items, needs ${s.minItems}`);
    const prefix = s.prefixItems ?? [];
    value.forEach((item, i) => {
      const itemSchema = i < prefix.length ? prefix[i] : s.items;
      if (itemSchema === false) errors.push(`${path}[${i}]: no item allowed here`);
      else if (itemSchema) errors.push(...validate(itemSchema, item, root, `${path}[${i}]`));
    });
  }
  if (actual === "object") {
    const obj = value as Record<string, unknown>;
    for (const key of s.required ?? []) {
      if (!Object.hasOwn(obj, key)) errors.push(`${at}: missing "${key}"`);
    }
    for (const [key, item] of Object.entries(obj)) {
      const keyPath = path ? `${path}.${key}` : key;
      if (s.propertyNames) errors.push(...validate(s.propertyNames, key, root, `${keyPath} (name)`));
      const propSchema = s.properties && Object.hasOwn(s.properties, key) ? s.properties[key] : s.additionalProperties;
      if (propSchema === false) errors.push(`${at}: unexpected "${key}"`);
      else if (propSchema) errors.push(...validate(propSchema, item, root, keyPath));
    }
  }
  return errors;
}

function doc(text: string | undefined, indent: string): string {
  return text ? `${indent}/** ${text.replace(/\*\//g, "*\\/")} */\n` : "";
}

function typeFor(schema: Schema, root: Schema, indent: string): string {
  checkKeywords(schema, "");
  if (schema.$ref) {
    deref(schema, root);
    return schema.$ref.slice("#/$defs/".length);
  }
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map((e) => JSON.stringify(e)).join(" | ");
  if (!schema.type) throw new Error("schema: a type needs type, enum, const or $ref");
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  return types.map((t) => {
    switch (t) {
      case "string": case "boolean": case "null": return t;
      case "number": case "integer": return "number";
      case "array": {
        if (schema.prefixItems && schema.items === false) {
          return `[${schema.prefixItems.map((p) => typeFor(p, root, indent)).join(", ")}]`;
        }
        if (!schema.items) throw new Error("schema: an array needs items");
        const item = typeFor(schema.items, root, indent);
        return /[ |]/.test(item) ? `(${item})[]` : `${item}[]`;
      }
      case "object": {
        const inner = indent + "  ";
        const required = new Set(schema.required ?? []);
        let body = "";
        for (const [key, prop] of Object.entries(schema.properties ?? {})) {
          body += doc(prop.description, inner);
          body += `${inner}${key}${required.has(key) ? "" : "?"}: ${typeFor(prop, root, inner)};\n`;
        }
        if (schema.additionalProperties) {
          body += `${inner}[key: string]: ${typeFor(schema.additionalProperties, root, inner)};\n`;
        }
        return `{\n${body}${indent}}`;
      }
    }
  }).join(" | ");
}

/** The TypeScript source of `schema`'s types: its title's type, one per $defs entry, and the
 *  schema itself as `constName`, for `validate` at load. */
export function generateTypes(schema: Schema, source: string, constName: string): string {
  if (!schema.title) throw new Error("schema: the root needs a title, the name of its type");
  let out = `// Generated from ${source} by \`cd web && bun run schema:gen\`. Do not edit: edit the schema.\n\n`;
  out += `import type { Schema } from "./schema";\n\n`;
  out += doc(schema.description, "");
  out += `export type ${schema.title} = ${typeFor(schema, schema, "")};\n`;
  for (const [name, def] of Object.entries(schema.$defs ?? {})) {
    out += `\n${doc(def.description, "")}export type ${name} = ${typeFor(def, schema, "")};\n`;
  }
  out += `\nexport const ${constName}: Schema = ${JSON.stringify(schema, null, 2)};\n`;
  return out;
}

// Parse source only: never evaluate repository expressions or interpolate values.
import ts from "typescript";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const result = {};
for (const [path, source] of Object.entries(JSON.parse(input))) {
  const tree = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const rows = [];
  function constant(node) {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
    if (ts.isParenthesizedExpression(node)) return constant(node.expression);
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const left = constant(node.left), right = constant(node.right);
      if (left !== undefined && right !== undefined) return left + right;
    }
    return undefined;
  }
  function record(node, value) {
    rows.push([tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1, value]);
  }
  function visit(node) {
    // Tagged templates may use raw semantics; retain raw fragments conservatively.
    if (ts.isTaggedTemplateExpression(node)) {
      const template = node.template;
      if (ts.isNoSubstitutionTemplateLiteral(template)) record(template, template.rawText ?? template.text);
      else {
        record(template.head, template.head.rawText ?? template.head.text);
        for (const span of template.templateSpans) {
          visit(span.expression);
          record(span.literal, span.literal.rawText ?? span.literal.text);
        }
      }
      visit(node.tag);
      return;
    }
    const value = constant(node);
    if (value !== undefined) {
      record(node, value);
      return; // A folded expression owns its children; count it only once.
    }
    if (ts.isTemplateExpression(node)) {
      record(node.head, node.head.text);
      for (const span of node.templateSpans) {
        visit(span.expression);
        record(span.literal, span.literal.text);
      }
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  result[path] = rows;
}
process.stdout.write(JSON.stringify(result));

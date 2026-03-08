const assert = require('assert');
const path = require('path');

const engine = require(path.join(__dirname, '..', 'canopy', 'ui', 'static', 'js', 'canopy-sheet.js'));

const source = [
    'title: Budget Ops',
    'columns: Item | Qty | Price | Total | Status | Flag',
    'row: Apples | 3 | 1.25 | =ROUND(B2*C2, 2) | =IF(B2>2, "restock", "ok") | =NOT(B2<3)',
    'row: Oranges | 2 | 2.00 | =ROUND(B3*C3, 2) | =IF(B3>2, "restock", "ok") | =OR(B3>4, C3>1)',
    'row: Pears | 5 | 0.80 | =ROUND(B4*C4, 2) | =IF(AND(B4>4, C4<1), "watch", "ok") | =ABS(C4-1)',
    'row: Stats | =MEDIAN(B2:B4) | =ROUND(STDDEV(B2:B4), 3) | =SUM(D2:D4) | =COUNT(B2:B4) | =A2&"-"&A4',
].join('\n');

const spec = engine.parseInlineSheetRows(source);
assert(spec, 'expected sheet spec');

const evaluated = engine.evaluateInlineSheetSpec(spec);
assert.strictEqual(evaluated.title, 'Budget Ops');
assert.strictEqual(evaluated.width, 6);
const layout = engine.buildColumnLayout(spec, evaluated);
assert.strictEqual(layout.length, 6);
assert(layout[1].kind === 'number', 'Qty column should compact as numeric');
assert(layout[2].kind === 'number', 'Price column should compact as numeric');
assert(layout[1].chars <= 12, 'numeric columns should stay compact');
assert(layout[0].wrap === false, 'short text label column should not wrap');
assert(layout[4].chars >= layout[1].chars, 'status text column should not be narrower than numeric columns');
assert.strictEqual(
    engine.serializeInlineSheetSpec(spec),
    source
);

assert.strictEqual(evaluated.rows[1][3].display, '3.75');
assert.strictEqual(evaluated.rows[1][4].display, 'restock');
assert.strictEqual(evaluated.rows[1][5].display, 'TRUE');

assert.strictEqual(evaluated.rows[2][3].display, '4');
assert.strictEqual(evaluated.rows[2][4].display, 'ok');
assert.strictEqual(evaluated.rows[2][5].display, 'TRUE');

assert.strictEqual(evaluated.rows[3][3].display, '4');
assert.strictEqual(evaluated.rows[3][4].display, 'watch');
assert.strictEqual(evaluated.rows[3][5].display, '0.2');

assert.strictEqual(evaluated.rows[4][1].display, '3');
assert.strictEqual(evaluated.rows[4][2].display, '1.528');
assert.strictEqual(evaluated.rows[4][3].display, '11.75');
assert.strictEqual(evaluated.rows[4][4].display, '3');
assert.strictEqual(evaluated.rows[4][5].display, 'Apples-Pears');

const cycleSpec = engine.parseInlineSheetRows([
    'columns: A | B',
    'row: =B2 | =A2',
].join('\n'));
const cycleEval = engine.evaluateInlineSheetSpec(cycleSpec);
assert.strictEqual(cycleEval.rows[1][0].display, '#CYCLE');
assert.strictEqual(cycleEval.rows[1][1].display, '#CYCLE');

console.log('inline sheet engine ok');

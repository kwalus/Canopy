(function(root, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory();
        return;
    }
    root.CanopySheetEngine = factory();
})(typeof self !== 'undefined' ? self : this, function() {
    'use strict';

    function columnLabel(index) {
        let label = '';
        let value = Number(index) + 1;
        while (value > 0) {
            const rem = (value - 1) % 26;
            label = String.fromCharCode(65 + rem) + label;
            value = Math.floor((value - 1) / 26);
        }
        return label;
    }

    function formatNumber(value) {
        const num = Number(value);
        if (!Number.isFinite(num)) return '#ERR';
        if (Math.abs(num - Math.round(num)) < 1e-9) return String(Math.round(num));
        return Number(num.toFixed(6)).toString();
    }

    function parseInlineSheetRows(source) {
        const spec = { title: '', columns: [], rows: [] };
        const lines = String(source || '').split(/\r?\n/);
        lines.forEach(function(line) {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('//')) return;
            const lower = trimmed.toLowerCase();
            if (lower.startsWith('title:')) {
                spec.title = trimmed.slice(6).trim();
                return;
            }
            if (lower.startsWith('columns:')) {
                spec.columns = trimmed.slice(8).split('|').map(function(part) { return part.trim(); });
                return;
            }
            if (lower.startsWith('row:')) {
                spec.rows.push(trimmed.slice(4).split('|').map(function(part) { return part.trim(); }));
            }
        });
        if (!spec.columns.length && !spec.rows.length) return null;
        return spec;
    }

    function buildInlineSheetMatrix(spec) {
        const matrix = [];
        if (Array.isArray(spec.columns) && spec.columns.length) {
            matrix.push(spec.columns.slice());
        }
        (spec.rows || []).forEach(function(row) {
            matrix.push(Array.isArray(row) ? row.slice() : []);
        });
        const width = Math.max.apply(null, matrix.map(function(row) { return row.length; }).concat([0]));
        matrix.forEach(function(row) {
            while (row.length < width) row.push('');
        });
        return { matrix: matrix, width: width };
    }

    function serializeInlineSheetSpec(spec) {
        const lines = [];
        const title = String((spec && spec.title) || '').trim();
        if (title) lines.push('title: ' + title);

        const columns = Array.isArray(spec && spec.columns)
            ? spec.columns.map(function(value) { return String(value == null ? '' : value).trim(); })
            : [];
        if (columns.length && columns.some(function(value) { return value !== ''; })) {
            lines.push('columns: ' + columns.join(' | '));
        }

        const rows = Array.isArray(spec && spec.rows) ? spec.rows : [];
        rows.forEach(function(row) {
            const normalized = Array.isArray(row)
                ? row.map(function(value) { return String(value == null ? '' : value).trim(); })
                : [];
            if (normalized.some(function(value) { return value !== ''; })) {
                lines.push('row: ' + normalized.join(' | '));
            }
        });

        return lines.join('\n').trim();
    }

    function cellRefToIndex(ref) {
        const match = /^([A-Z]+)(\d+)$/.exec(String(ref || '').trim().toUpperCase());
        if (!match) return null;
        let col = 0;
        for (let i = 0; i < match[1].length; i += 1) {
            col = (col * 26) + (match[1].charCodeAt(i) - 64);
        }
        return { row: Number(match[2]) - 1, col: col - 1 };
    }

    function tokenizeFormula(expr) {
        const tokens = [];
        let index = 0;
        while (index < expr.length) {
            const ch = expr[index];
            if (/\s/.test(ch)) {
                index += 1;
                continue;
            }

            const multiOp = expr.slice(index).match(/^(<=|>=|<>|!=)/);
            if (multiOp) {
                tokens.push({ type: 'op', value: multiOp[1] });
                index += multiOp[1].length;
                continue;
            }

            if ('+-*/^(),:<>=&'.includes(ch)) {
                tokens.push({
                    type: (ch === '(' || ch === ')' || ch === ',' || ch === ':') ? ch : 'op',
                    value: ch,
                });
                index += 1;
                continue;
            }

            const number = expr.slice(index).match(/^\d+(?:\.\d+)?/);
            if (number) {
                tokens.push({ type: 'number', value: Number(number[0]) });
                index += number[0].length;
                continue;
            }

            if (ch === '"' || ch === '\'') {
                let value = '';
                const quote = ch;
                index += 1;
                while (index < expr.length) {
                    const current = expr[index];
                    if (current === '\\' && index + 1 < expr.length) {
                        value += expr[index + 1];
                        index += 2;
                        continue;
                    }
                    if (current === quote) {
                        index += 1;
                        break;
                    }
                    value += current;
                    index += 1;
                }
                tokens.push({ type: 'string', value: value });
                continue;
            }

            const ident = expr.slice(index).match(/^[A-Za-z_][A-Za-z0-9_]*/);
            if (ident) {
                tokens.push({ type: 'ident', value: ident[0].toUpperCase() });
                index += ident[0].length;
                continue;
            }

            throw new Error('Unexpected token "' + ch + '"');
        }
        return tokens;
    }

    function isBlank(value) {
        return value === '' || value === null || typeof value === 'undefined';
    }

    function scalarValue(value) {
        if (Array.isArray(value)) return value.length ? scalarValue(value[0]) : '';
        return value;
    }

    function toNumberOrNull(value) {
        const actual = scalarValue(value);
        if (isBlank(actual)) return null;
        if (typeof actual === 'number') return Number.isFinite(actual) ? actual : null;
        if (typeof actual === 'boolean') return actual ? 1 : 0;
        const parsed = Number(actual);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function toNumber(value) {
        const parsed = toNumberOrNull(value);
        return parsed == null ? 0 : parsed;
    }

    function toBoolean(value) {
        const actual = scalarValue(value);
        if (Array.isArray(actual)) return actual.length > 0;
        if (typeof actual === 'boolean') return actual;
        if (typeof actual === 'number') return actual !== 0;
        if (actual == null) return false;
        const text = String(actual).trim().toLowerCase();
        if (!text) return false;
        if (text === 'false' || text === 'no' || text === 'off') return false;
        if (text === 'true' || text === 'yes' || text === 'on') return true;
        const numeric = Number(text);
        if (Number.isFinite(numeric)) return numeric !== 0;
        return true;
    }

    function flattenValues(values) {
        return (values || []).reduce(function(acc, value) {
            if (Array.isArray(value)) {
                return acc.concat(flattenValues(value));
            }
            acc.push(value);
            return acc;
        }, []);
    }

    function numericValues(values) {
        return flattenValues(values).map(toNumberOrNull).filter(function(value) {
            return value != null && Number.isFinite(value);
        });
    }

    function median(values) {
        if (!values.length) return 0;
        const sorted = values.slice().sort(function(a, b) { return a - b; });
        const mid = Math.floor(sorted.length / 2);
        if (sorted.length % 2) return sorted[mid];
        return (sorted[mid - 1] + sorted[mid]) / 2;
    }

    function sampleStdDev(values) {
        if (values.length < 2) return 0;
        const mean = values.reduce(function(sum, value) { return sum + value; }, 0) / values.length;
        const variance = values.reduce(function(sum, value) {
            const delta = value - mean;
            return sum + (delta * delta);
        }, 0) / (values.length - 1);
        return Math.sqrt(variance);
    }

    function roundTo(value, digits) {
        const num = toNumber(value);
        const places = Math.max(-9, Math.min(9, Math.trunc(toNumber(digits))));
        const factor = Math.pow(10, places);
        if (places >= 0) {
            return Math.round(num * factor) / factor;
        }
        const inverse = Math.pow(10, -places);
        return Math.round(num / inverse) * inverse;
    }

    function compareValues(left, right, operator) {
        const leftNum = toNumberOrNull(left);
        const rightNum = toNumberOrNull(right);
        let comparison;
        if (leftNum != null && rightNum != null) {
            comparison = leftNum === rightNum ? 0 : (leftNum < rightNum ? -1 : 1);
        } else {
            const leftText = String(scalarValue(left));
            const rightText = String(scalarValue(right));
            comparison = leftText === rightText ? 0 : (leftText < rightText ? -1 : 1);
        }
        if (operator === '=' || operator === '==') return comparison === 0;
        if (operator === '<>' || operator === '!=') return comparison !== 0;
        if (operator === '<') return comparison < 0;
        if (operator === '>') return comparison > 0;
        if (operator === '<=') return comparison <= 0;
        if (operator === '>=') return comparison >= 0;
        throw new Error('Unsupported comparator ' + operator);
    }

    function callFunction(name, args) {
        const upper = String(name || '').toUpperCase();
        const numbers = numericValues(args);
        if (upper === 'SUM') {
            return numbers.reduce(function(sum, value) { return sum + value; }, 0);
        }
        if (upper === 'AVG' || upper === 'AVERAGE') {
            if (!numbers.length) return 0;
            return numbers.reduce(function(sum, value) { return sum + value; }, 0) / numbers.length;
        }
        if (upper === 'MIN') return numbers.length ? Math.min.apply(null, numbers) : 0;
        if (upper === 'MAX') return numbers.length ? Math.max.apply(null, numbers) : 0;
        if (upper === 'COUNT') return numbers.length;
        if (upper === 'ABS') return Math.abs(toNumber(args[0]));
        if (upper === 'ROUND') return roundTo(args[0], typeof args[1] === 'undefined' ? 0 : args[1]);
        if (upper === 'MEDIAN') return median(numbers);
        if (upper === 'STDDEV' || upper === 'STDEV') return sampleStdDev(numbers);
        if (upper === 'IF') {
            return toBoolean(args[0]) ? scalarValue(args[1]) : scalarValue(args[2]);
        }
        if (upper === 'AND') {
            return flattenValues(args).every(function(value) { return toBoolean(value); });
        }
        if (upper === 'OR') {
            return flattenValues(args).some(function(value) { return toBoolean(value); });
        }
        if (upper === 'NOT') {
            return !toBoolean(args[0]);
        }
        throw new Error('Unsupported function ' + upper);
    }

    function evaluateInlineSheetSpec(spec) {
        const built = buildInlineSheetMatrix(spec || {});
        const matrix = built.matrix;
        const width = built.width;
        const memo = new Map();
        const visiting = new Set();

        function literalCell(raw) {
            const text = String(raw == null ? '' : raw).trim();
            if (!text) return { value: '', display: '', kind: 'empty' };
            if (/^(true|false)$/i.test(text)) {
                const bool = /^true$/i.test(text);
                return { value: bool, display: bool ? 'TRUE' : 'FALSE', kind: 'boolean' };
            }
            if (/^-?\d+(?:\.\d+)?$/.test(text)) {
                const num = Number(text);
                return { value: num, display: formatNumber(num), kind: 'number' };
            }
            return { value: text, display: text, kind: 'text' };
        }

        function resolveCell(rowIndex, colIndex) {
            const key = String(rowIndex) + ':' + String(colIndex);
            if (memo.has(key)) return memo.get(key);
            if (visiting.has(key)) {
                return { value: '#CYCLE', display: '#CYCLE', kind: 'error', error: 'Cycle detected' };
            }
            visiting.add(key);
            const raw = (matrix[rowIndex] && matrix[rowIndex][colIndex] != null) ? String(matrix[rowIndex][colIndex]).trim() : '';
            let result;
            if (!raw.startsWith('=')) {
                result = literalCell(raw);
            } else {
                try {
                    const value = evaluateFormula(raw.slice(1));
                    if (typeof value === 'boolean') {
                        result = {
                            value: value,
                            display: value ? 'TRUE' : 'FALSE',
                            kind: 'boolean',
                            formula: raw,
                        };
                    } else if (typeof value === 'number') {
                        result = {
                            value: value,
                            display: formatNumber(value),
                            kind: 'number',
                            formula: raw,
                        };
                    } else if (typeof value === 'string' && value.charAt(0) === '#') {
                        result = {
                            value: value,
                            display: value,
                            kind: 'error',
                            formula: raw,
                            error: value,
                        };
                    } else if (isBlank(value)) {
                        result = { value: '', display: '', kind: 'empty', formula: raw };
                    } else {
                        result = {
                            value: String(value),
                            display: String(value),
                            kind: 'text',
                            formula: raw,
                        };
                    }
                } catch (error) {
                    result = {
                        value: '#ERR',
                        display: '#ERR',
                        kind: 'error',
                        formula: raw,
                        error: error && error.message ? error.message : 'Formula error',
                    };
                }
            }
            visiting.delete(key);
            memo.set(key, result);
            return result;
        }

        function rangeValues(startRef, endRef) {
            const start = cellRefToIndex(startRef);
            const end = cellRefToIndex(endRef);
            if (!start || !end) return [];
            const rowStart = Math.min(start.row, end.row);
            const rowEnd = Math.max(start.row, end.row);
            const colStart = Math.min(start.col, end.col);
            const colEnd = Math.max(start.col, end.col);
            const values = [];
            for (let row = rowStart; row <= rowEnd; row += 1) {
                for (let col = colStart; col <= colEnd; col += 1) {
                    values.push(resolveCell(row, col).value);
                }
            }
            return values;
        }

        function evaluateFormula(expr) {
            const tokens = tokenizeFormula(expr);
            let cursor = 0;

            function current() {
                return tokens[cursor] || null;
            }

            function eat(type, value) {
                const token = current();
                if (!token || token.type !== type || (typeof value !== 'undefined' && token.value !== value)) {
                    throw new Error('Expected ' + (value || type));
                }
                cursor += 1;
                return token;
            }

            function parseFunctionArg() {
                const first = current();
                const second = tokens[cursor + 1];
                const third = tokens[cursor + 2];
                if (
                    first && first.type === 'ident' &&
                    second && second.type === ':' &&
                    third && third.type === 'ident' &&
                    cellRefToIndex(first.value) &&
                    cellRefToIndex(third.value)
                ) {
                    cursor += 3;
                    return rangeValues(first.value, third.value);
                }
                return parseComparison();
            }

            function parsePrimary() {
                const token = current();
                if (!token) throw new Error('Unexpected end of formula');
                if (token.type === 'number') {
                    cursor += 1;
                    return token.value;
                }
                if (token.type === 'string') {
                    cursor += 1;
                    return token.value;
                }
                if (token.type === 'ident') {
                    cursor += 1;
                    const ident = token.value;
                    if (ident === 'TRUE') return true;
                    if (ident === 'FALSE') return false;
                    if (current() && current().type === '(') {
                        eat('(');
                        const args = [];
                        while (current() && current().type !== ')') {
                            args.push(parseFunctionArg());
                            if (current() && current().type === ',') eat(',');
                        }
                        eat(')');
                        return callFunction(ident, args);
                    }
                    const ref = cellRefToIndex(ident);
                    if (ref) return resolveCell(ref.row, ref.col).value;
                    return 0;
                }
                if (token.type === '(') {
                    eat('(');
                    const value = parseComparison();
                    eat(')');
                    return value;
                }
                throw new Error('Unsupported formula token');
            }

            function parseUnary() {
                const token = current();
                if (token && token.type === 'op' && token.value === '+') {
                    eat('op', '+');
                    return parseUnary();
                }
                if (token && token.type === 'op' && token.value === '-') {
                    eat('op', '-');
                    return -toNumber(parseUnary());
                }
                return parsePrimary();
            }

            function parsePower() {
                let value = parseUnary();
                while (current() && current().type === 'op' && current().value === '^') {
                    eat('op', '^');
                    value = Math.pow(toNumber(value), toNumber(parseUnary()));
                }
                return value;
            }

            function parseConcat() {
                let value = parsePower();
                while (current() && current().type === 'op' && current().value === '&') {
                    eat('op', '&');
                    value = String(scalarValue(value)) + String(scalarValue(parsePower()));
                }
                return value;
            }

            function parseTerm() {
                let value = parseConcat();
                while (current() && current().type === 'op' && (current().value === '*' || current().value === '/')) {
                    const operator = current().value;
                    eat('op', operator);
                    const rhs = parseConcat();
                    value = operator === '*' ? toNumber(value) * toNumber(rhs) : toNumber(value) / toNumber(rhs);
                }
                return value;
            }

            function parseArithmetic() {
                let value = parseTerm();
                while (current() && current().type === 'op' && (current().value === '+' || current().value === '-')) {
                    const operator = current().value;
                    eat('op', operator);
                    const rhs = parseTerm();
                    value = operator === '+' ? toNumber(value) + toNumber(rhs) : toNumber(value) - toNumber(rhs);
                }
                return value;
            }

            function parseComparison() {
                let value = parseArithmetic();
                while (
                    current() &&
                    current().type === 'op' &&
                    ['=', '==', '<>', '!=', '<', '>', '<=', '>='].indexOf(current().value) >= 0
                ) {
                    const operator = current().value;
                    eat('op', operator);
                    value = compareValues(value, parseArithmetic(), operator);
                }
                return value;
            }

            const result = parseComparison();
            if (cursor !== tokens.length) throw new Error('Trailing formula tokens');
            return result;
        }

        const rows = matrix.map(function(row, rowIndex) {
            return row.map(function(_, colIndex) {
                return resolveCell(rowIndex, colIndex);
            });
        });

        return {
            title: spec && spec.title ? spec.title : '',
            width: width,
            rows: rows,
        };
    }

    return {
        buildInlineSheetMatrix: buildInlineSheetMatrix,
        cellRefToIndex: cellRefToIndex,
        columnLabel: columnLabel,
        evaluateInlineSheetSpec: evaluateInlineSheetSpec,
        formatNumber: formatNumber,
        parseInlineSheetRows: parseInlineSheetRows,
        serializeInlineSheetSpec: serializeInlineSheetSpec,
        tokenizeFormula: tokenizeFormula,
    };
});

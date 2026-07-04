"""
A minimal parser for the restricted subset of Lua table syntax used by
wiki.warframe.com's Module:Baro/data page. Not a general Lua parser -
just enough to turn:

    return {
        ["Items"] = {
            ["Item Name"] = {
                CreditCost = 100000,
                OfferingDates = { "2022-07-29", "2022-08-12" },
                ...
            },
            ...
        },
    }

into equivalent Python dicts/lists.
"""

import re

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<comment>--\[\[.*?\]\]|--[^\n]*) |
        (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*') |
        (?P<number>-?\d+\.\d+|-?\d+) |
        (?P<ident>[A-Za-z_][A-Za-z0-9_]*) |
        (?P<lbrace>\{) |
        (?P<rbrace>\}) |
        (?P<lbracket>\[) |
        (?P<rbracket>\]) |
        (?P<equals>=) |
        (?P<comma>,)
    )
    """,
    re.VERBOSE | re.DOTALL,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens = []
    pos = 0
    length = len(text)
    while pos < length:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            # skip unrecognized character (stray whitespace/newline already
            # consumed by \s*, this guards against anything else odd)
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        if kind == "comment":
            continue
        tokens.append((kind, m.group(kind)))
    return tokens


def _unescape(raw: str) -> str:
    inner = raw[1:-1]
    return inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise SyntaxError(f"expected {kind}, got {tok} at token {self.pos}")
        return tok

    def parse_value(self):
        tok = self.peek()
        if tok is None:
            raise SyntaxError("unexpected end of input")
        if tok[0] == "lbrace":
            return self.parse_table()
        if tok[0] == "string":
            self.next()
            return _unescape(tok[1])
        if tok[0] == "number":
            self.next()
            return float(tok[1]) if "." in tok[1] else int(tok[1])
        if tok[0] == "ident":
            self.next()
            if tok[1] == "true":
                return True
            if tok[1] == "false":
                return False
            if tok[1] == "nil":
                return None
            return tok[1]  # bare word, treated as a string
        raise SyntaxError(f"unexpected token {tok} at token {self.pos}")

    def parse_table(self):
        self.expect("lbrace")
        result_dict = {}
        result_list = []
        is_dict = None

        while True:
            tok = self.peek()
            if tok is None:
                raise SyntaxError("unterminated table")
            if tok[0] == "rbrace":
                self.next()
                break

            if tok[0] == "lbracket":
                self.next()
                key_tok = self.next()
                key = _unescape(key_tok[1]) if key_tok[0] == "string" else key_tok[1]
                self.expect("rbracket")
                self.expect("equals")
                value = self.parse_value()
                result_dict[key] = value
                is_dict = True
            elif (
                tok[0] == "ident"
                and self.pos + 1 < len(self.tokens)
                and self.tokens[self.pos + 1][0] == "equals"
            ):
                key_tok = self.next()
                self.expect("equals")
                value = self.parse_value()
                result_dict[key_tok[1]] = value
                is_dict = True
            else:
                value = self.parse_value()
                result_list.append(value)
                is_dict = False if is_dict is None else is_dict

            nxt = self.peek()
            if nxt is None:
                raise SyntaxError("unterminated table")
            if nxt[0] == "comma":
                self.next()
            elif nxt[0] == "rbrace":
                continue
            else:
                raise SyntaxError(f"expected ',' or '}}', got {nxt} at token {self.pos}")

        return result_dict if is_dict else result_list


def parse_lua_return_table(lua_source: str) -> dict:
    """
    Parse a Lua chunk of the form `return { ["Key"] = ... }` (with optional
    leading comments) into an equivalent Python dict. The Baro data module
    always returns a keyed table at the top level, so callers can rely on
    a dict despite parse_value() being generic over all Lua value types.
    """
    tokens = _tokenize(lua_source)
    # skip a leading 'return' identifier if present
    idx = 0
    if idx < len(tokens) and tokens[idx] == ("ident", "return"):
        idx += 1
    parser = _Parser(tokens[idx:])
    result = parser.parse_value()
    if not isinstance(result, dict):
        raise SyntaxError(f"expected top-level table to be a keyed table (dict), got {type(result).__name__}")
    return result

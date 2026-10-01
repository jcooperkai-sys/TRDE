"""SQL tokenizer."""

from .errors import ParseError

KEYWORDS = set("""
ALL AND AS ASC BEGIN BETWEEN BY CASE COMMIT COUNT CREATE CROSS DEFAULT DELETE
DESC DISTINCT DROP ELSE END EXISTS EXPLAIN FROM FULL GROUP HAVING IF IN INDEX
INNER INSERT INTO IS JOIN KEY LEFT LIKE LIMIT NOT NULL OFFSET ON OR ORDER
OUTER PRIMARY REFERENCES RIGHT ROLLBACK SELECT SET TABLE THEN TRANSACTION
UNIQUE UPDATE USING VALUES WHEN WHERE
""".split())

# Multi-character operators, longest first so the scanner matches greedily.
OPERATORS = [
    "<>", "<=", ">=", "==", "!=", "||",
    "(", ")", ",", ".", ";", "+", "-", "*", "/", "%", "<", ">", "=",
]

TK_IDENT = "IDENT"
TK_KEYWORD = "KEYWORD"
TK_NUMBER = "NUMBER"
TK_STRING = "STRING"
TK_BLOB = "BLOB"
TK_PARAM = "PARAM"
TK_OP = "OP"
TK_EOF = "EOF"


class Token(object):
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind, value, pos):
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self):
        return "Token(%s, %r)" % (self.kind, self.value)


def _is_ident_start(ch):
    return ch.isalpha() or ch == "_"


def _is_ident_char(ch):
    return ch.isalnum() or ch == "_" or ch == "$"


def tokenize(text):
    tokens = []
    i = 0
    n = len(text)
    param_index = 0
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ParseError("unterminated block comment", i)
            i = j + 2
            continue
        if ch == "'":
            j = i + 1
            out = []
            while True:
                if j >= n:
                    raise ParseError("unterminated string literal", i)
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        out.append("'")
                        j += 2
                        continue
                    break
                out.append(text[j])
                j += 1
            tokens.append(Token(TK_STRING, "".join(out), i))
            i = j + 1
            continue
        if ch == '"' or ch == "`" or ch == "[":
            close = {'"': '"', "`": "`", "[": "]"}[ch]
            j = text.find(close, i + 1)
            if j < 0:
                raise ParseError("unterminated quoted identifier", i)
            tokens.append(Token(TK_IDENT, text[i + 1:j], i))
            i = j + 1
            continue
        if (ch in "xX") and i + 1 < n and text[i + 1] == "'":
            j = text.find("'", i + 2)
            if j < 0:
                raise ParseError("unterminated blob literal", i)
            body = text[i + 2:j]
            try:
                blob = bytes(bytearray.fromhex(body))
            except ValueError:
                raise ParseError("invalid hex in blob literal", i)
            tokens.append(Token(TK_BLOB, blob, i))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            seen_dot = False
            seen_exp = False
            while j < n:
                c = text[j]
                if c.isdigit():
                    j += 1
                elif c == "." and not seen_dot and not seen_exp:
                    seen_dot = True
                    j += 1
                elif c in "eE" and not seen_exp and j + 1 < n and (text[j + 1].isdigit() or text[j + 1] in "+-"):
                    seen_exp = True
                    j += 2
                else:
                    break
            raw = text[i:j]
            value = float(raw) if (seen_dot or seen_exp) else int(raw)
            tokens.append(Token(TK_NUMBER, value, i))
            i = j
            continue
        if _is_ident_start(ch):
            j = i
            while j < n and _is_ident_char(text[j]):
                j += 1
            word = text[i:j]
            upper = word.upper()
            kind = TK_KEYWORD if upper in KEYWORDS else TK_IDENT
            tokens.append(Token(kind, upper if kind == TK_KEYWORD else word, i))
            i = j
            continue
        if ch == "?":
            tokens.append(Token(TK_PARAM, param_index, i))
            param_index += 1
            i += 1
            continue
        for op in OPERATORS:
            if text.startswith(op, i):
                tokens.append(Token(TK_OP, op, i))
                i += len(op)
                break
        else:
            raise ParseError("unexpected character %r" % ch, i)
    tokens.append(Token(TK_EOF, None, n))
    return tokens

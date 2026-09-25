#!/usr/bin/env python3
"""
code_outline.py —— 纯 Python 标准库实现的代码结构提取工具（单文件，零依赖）

功能
----
1. 提取源文本中所有函数 / 类定义：名称、参数列表、起止行号、嵌套层级；
2. 输出“定义清单”与“嵌套结构树”；
3. 词法层面正确识别：单 / 双引号字符串、三引号字符串、行注释（#、//）、
   块注释（/* ... */）、反斜杠转义；字符串与注释里的关键字、括号一律忽略；
4. 括号配对检查：报告不配对括号所在的行、列及具体字符；
5. 字符串未闭合、块注释未结束的定位报告。

用法
----
    python3 code_outline.py 源码文件
    python3 code_outline.py --demo      # 分析内置样例（正常样例 + 错误样例）
    cat 源码文件 | python3 code_outline.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------


@dataclass
class ScanError:
    line: int
    col: int
    message: str


@dataclass
class Definition:
    kind: str               # 'function' | 'async function' | 'class'
    name: str
    params: str             # 参数列表（类为基类列表，可能为空）
    start_line: int
    indent: int             # 定义所在行的缩进宽度（tab 按 8 展开）
    end_line: int = 0
    depth: int = 0
    children: list = field(default_factory=list)


# ----------------------------------------------------------------------
# 第一遍：词法扫描，把字符串 / 注释“打码”为空格（保留换行，保证行列号不变）
# ----------------------------------------------------------------------

_NORMAL, _STRING, _LINE_COMMENT, _BLOCK_COMMENT = range(4)


def _is_raw_prefix(text: str, pos: int) -> bool:
    """判断 pos 处的引号是否带 r/R 前缀（raw 字符串不处理反斜杠转义）。"""
    j = pos - 1
    prefix = []
    while j >= 0 and text[j] in 'rRbBuUfF':
        prefix.append(text[j])
        j -= 1
    if not prefix:
        return False
    if j >= 0 and (text[j].isalnum() or text[j] == '_'):
        return False  # 字母属于前面的标识符，不是字符串前缀
    return 'r' in prefix or 'R' in prefix


def mask_source(text: str):
    """返回 (masked_text, errors)。

    masked_text 与 text 完全等长：字符串与注释的字符被替换为空格
    （换行符除外），因此后续在 masked_text 上做括号匹配、关键字识别时，
    字符串 / 注释里的内容天然被忽略，且行列号与原文一一对应。
    """
    masked = list(text)
    errors: list[ScanError] = []
    n = len(text)
    i = 0
    line = col = 1

    def consume(k: int = 1, blank: bool = True) -> None:
        nonlocal i, line, col
        for _ in range(k):
            if i >= n:
                return
            ch = text[i]
            if blank and ch != '\n':
                masked[i] = ' '
            if ch == '\n':
                line, col = line + 1, 1
            else:
                col += 1
            i += 1

    state = _NORMAL
    quote = ''
    triple = False
    raw = False
    start_pos = (1, 1)

    while i < n:
        ch = text[i]
        if state == _NORMAL:
            if ch == '#':
                state = _LINE_COMMENT
                consume()
            elif ch == '/' and text.startswith('//', i):
                state = _LINE_COMMENT
                consume(2)
            elif ch == '/' and text.startswith('/*', i):
                state = _BLOCK_COMMENT
                start_pos = (line, col)
                consume(2)
            elif ch == '"' or ch == "'":
                raw = _is_raw_prefix(text, i)
                triple = text.startswith(ch * 3, i)
                state = _STRING
                quote = ch
                start_pos = (line, col)
                consume(3 if triple else 1)
            else:
                consume(blank=False)
        elif state == _STRING:
            if ch == '\\' and not raw:
                consume(2)          # 转义序列整体跳过（\' \" \\ \n 等）
            elif triple and text.startswith(quote * 3, i):
                consume(3)
                state = _NORMAL
            elif not triple and ch == quote:
                consume()
                state = _NORMAL
            elif not triple and ch == '\n':
                errors.append(ScanError(
                    start_pos[0], start_pos[1],
                    f"字符串未闭合（{quote} 从这里开始）"))
                state = _NORMAL
                consume(blank=False)
            else:
                consume()
        elif state == _LINE_COMMENT:
            if ch == '\n':
                state = _NORMAL
                consume(blank=False)
            else:
                consume()
        else:  # _BLOCK_COMMENT
            if text.startswith('*/', i):
                consume(2)
                state = _NORMAL
            else:
                consume()

    if state == _STRING:
        errors.append(ScanError(
            start_pos[0], start_pos[1],
            f"字符串未闭合（{quote} 从这里开始，直到文件结束）"))
    elif state == _BLOCK_COMMENT:
        errors.append(ScanError(
            start_pos[0], start_pos[1],
            "块注释未结束（/* 从这里开始，直到文件结束）"))
    return ''.join(masked), errors


# ----------------------------------------------------------------------
# 第二遍：在“打码”后的文本上做括号配对检查
# ----------------------------------------------------------------------

_OPEN_TO_CLOSE = {'(': ')', '[': ']', '{': '}'}
_CLOSE_TO_OPEN = {v: k for k, v in _OPEN_TO_CLOSE.items()}


def check_brackets(masked: str) -> list[ScanError]:
    """栈式配对。masked 中只剩代码字符，字符串/注释里的括号已被剔除。"""
    errors: list[ScanError] = []
    stack: list[tuple[str, int, int]] = []
    line = col = 1
    for ch in masked:
        if ch in _OPEN_TO_CLOSE:
            stack.append((ch, line, col))
        elif ch in _CLOSE_TO_OPEN:
            if not stack:
                errors.append(ScanError(
                    line, col, f"闭括号 '{ch}' 没有对应的开括号"))
            else:
                open_ch, open_line, open_col = stack.pop()
                if _CLOSE_TO_OPEN[ch] != open_ch:
                    errors.append(ScanError(
                        line, col,
                        f"闭括号 '{ch}' 与第 {open_line} 行第 {open_col} 列的"
                        f"开括号 '{open_ch}' 不匹配"))
        if ch == '\n':
            line, col = line + 1, 1
        else:
            col += 1
    for open_ch, open_line, open_col in reversed(stack):
        errors.append(ScanError(
            open_line, open_col, f"开括号 '{open_ch}' 未闭合"))
    return errors


# ----------------------------------------------------------------------
# 第三遍：在“打码”后的文本上识别 def / class，从原文切出参数列表
# ----------------------------------------------------------------------

_DEF_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<async>async[ \t]+)?def[ \t]+'
    r'(?P<name>[A-Za-z_]\w*)[ \t]*\(')
_CLASS_RE = re.compile(
    r'^(?P<indent>[ \t]*)class[ \t]+(?P<name>[A-Za-z_]\w*)')


def _capture_parens(masked: str, original: str, open_off: int) -> str:
    """从 masked[open_off] 处的 '(' 出发找配对的 ')'，从原文切出参数文本。

    参数可以跨行、可以含字符串/注释/嵌套括号——masked 里只剩真实括号，
    计数配对即可；切文本时用相同偏移量切 original，保留默认值原貌。
    """
    depth = 0
    for i in range(open_off, len(masked)):
        ch = masked[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return ' '.join(original[open_off + 1:i].split())
    # 括号未闭合：退化为截取到本行末尾，避免吞掉后续所有内容
    eol = original.find('\n', open_off)
    rest = original[open_off + 1:eol if eol != -1 else len(original)]
    return ' '.join(rest.split()) + ' ...'


def extract_definitions(masked: str, original: str) -> list[Definition]:
    line_offsets = [0]
    for m in re.finditer('\n', original):
        line_offsets.append(m.end())

    defs: list[Definition] = []
    for idx, line_text in enumerate(masked.split('\n')):
        m = _DEF_RE.match(line_text)
        if m:
            kind = 'async function' if m.group('async') else 'function'
            open_off = line_offsets[idx] + m.end() - 1
            params = _capture_parens(masked, original, open_off)
        else:
            m = _CLASS_RE.match(line_text)
            if not m:
                continue
            kind = 'class'
            params = ''
            j = line_offsets[idx] + m.end()
            while j < len(masked) and masked[j] in ' \t':
                j += 1
            if j < len(masked) and masked[j] == '(':
                params = _capture_parens(masked, original, j)
        defs.append(Definition(
            kind=kind,
            name=m.group('name'),
            params=params,
            start_line=idx + 1,
            indent=len(m.group('indent').expandtabs(8)),
        ))
    return defs


# ----------------------------------------------------------------------
# 第四遍：用缩进计算每个定义的结束行，并构建嵌套树
# ----------------------------------------------------------------------

def _compute_end_lines(defs: list[Definition], masked: str) -> None:
    """定义的函数体结束于“下一个缩进 <= 自身缩进的有效代码行”之前。

    有效行 = 打码后仍有内容的行（纯字符串/注释/空行不算），
    因此多行字符串、块注释不会干扰缩进判断。
    """
    effective: list[tuple[int, int]] = []  # (行号, 缩进宽度)
    for idx, line_text in enumerate(masked.split('\n')):
        if line_text.strip():
            lead = line_text[:len(line_text) - len(line_text.lstrip(' \t'))]
            effective.append((idx + 1, len(lead.expandtabs(8))))
    for d in defs:
        prev = d.start_line
        for line_no, width in effective:
            if line_no <= d.start_line:
                continue
            if width <= d.indent:
                break
            prev = line_no
        d.end_line = prev


def _build_tree(defs: list[Definition]) -> list[Definition]:
    """按源码顺序用栈构建嵌套树：缩进更大的定义是最近未闭合定义的子节点。"""
    roots: list[Definition] = []
    stack: list[Definition] = []
    for d in defs:
        while stack and stack[-1].indent >= d.indent:
            stack.pop()
        if stack:
            d.depth = stack[-1].depth + 1
            stack[-1].children.append(d)
        else:
            d.depth = 0
            roots.append(d)
        stack.append(d)
    return roots


# ----------------------------------------------------------------------
# 报告输出
# ----------------------------------------------------------------------

_KIND_LABEL = {'function': '函数', 'async function': '异步函数', 'class': '类'}


def _signature(d: Definition) -> str:
    if d.kind == 'class' and not d.params:
        return f"{_KIND_LABEL[d.kind]} {d.name}"
    return f"{_KIND_LABEL[d.kind]} {d.name}({d.params})"


def format_report(defs: list[Definition], roots: list[Definition],
                  errors: list[ScanError]) -> str:
    out = ['=' * 24 + ' 定义清单 ' + '=' * 24]
    if not defs:
        out.append('（未找到任何定义）')
    for d in defs:
        parens = '' if (d.kind == 'class' and not d.params) else f"({d.params})"
        out.append(f"[{_KIND_LABEL[d.kind]}] {d.name}{parens}"
                   f"    行 {d.start_line}-{d.end_line}    嵌套层级 {d.depth}")
    out.append('')
    out.append('=' * 24 + ' 嵌套结构 ' + '=' * 24)

    def walk(node: Definition) -> None:
        out.append('  ' * node.depth + _signature(node)
                   + f"  [行 {node.start_line}-{node.end_line}]")
        for child in node.children:
            walk(child)

    if roots:
        for r in roots:
            walk(r)
    else:
        out.append('（空）')
    out.append('')
    out.append('=' * 24 + ' 错误定位 ' + '=' * 24)
    if errors:
        for e in sorted(errors, key=lambda e: (e.line, e.col)):
            out.append(f"第 {e.line} 行第 {e.col} 列：{e.message}")
    else:
        out.append('（没有发现问题）')
    return '\n'.join(out)


def analyze(source: str) -> str:
    masked, lex_errors = mask_source(source)
    bracket_errors = check_brackets(masked)
    defs = extract_definitions(masked, source)
    _compute_end_lines(defs, masked)
    roots = _build_tree(defs)
    return format_report(defs, roots, lex_errors + bracket_errors)


# ----------------------------------------------------------------------
# 内置样例
# ----------------------------------------------------------------------

DEMO_GOOD = '''#!/usr/bin/env python3
"""模块文档字符串：def ghost(): 和 class Phantom: 都不算定义"""

import os  # 行注释里的 def fake(): 也不算

TEXT = "字符串里的 def s1(): 与 class S2: 还有括号 ((( 都不算"
QUOTE = '转义引号 \\' 之后仍是同一个字符串 def s3():'
RAW = r"raw 字符串里 \\ 不是转义 def s4():"
MULTI = """
三引号字符串可以跨行
def inner_ghost(a, b):
    class DeepGhost: pass
"""

def top_level(a, b=10, *args, **kwargs):
    """文档字符串 class Nothing: def noop():"""
    def local_fn(x, y={"k": [1, 2]}):
        return x + y  # 注释里的括号 ))) 不影响配对
    data = {"key": (1, 2)}
    return local_fn(a, b)

class Outer(Base, metaclass=type):
    """类文档：def method_ghost(self):"""

    def method_one(self, x):
        def helper():
            return x * 2
        return helper()

    class Inner:
        def deep(self, a, b, c):
            pass

async def aio_fetch(url, timeout=5):
    text = "/* 这只是普通字符串 */ def nope():"
    return text
'''

DEMO_BAD = '''def broken(a, b:
    return a + b

x = (1 + 2]
z = ((1, 2)

s = "这个字符串没有闭合
ok = '这一行恢复正常'

/* 块注释开始
def hidden_in_comment():
    pass
注释永远不会结束，直到文件末尾
'''


def main(argv: list[str]) -> int:
    if '--demo' in argv:
        print('#' * 22 + ' 样例一：正常代码 ' + '#' * 22)
        print(analyze(DEMO_GOOD))
        print()
        print('#' * 22 + ' 样例二：含错误代码 ' + '#' * 22)
        print(analyze(DEMO_BAD))
        return 0
    if len(argv) > 1:
        with open(argv[1], 'r', encoding='utf-8') as f:
            source = f.read()
    else:
        source = sys.stdin.read()
    print(analyze(source))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))

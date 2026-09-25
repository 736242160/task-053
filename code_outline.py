#!/usr/bin/env python3
"""code_outline.py — 纯标准库的源码结构提取器（单文件，无第三方/解析库依赖）。

功能：
  1. 提取所有函数与类定义：名称、参数列表、起止行号、嵌套层级；
  2. 输出扁平定义清单与嵌套结构树；
  3. 词法阶段正确跳过字符串（单/双/三引号、转义字符）、行注释、块注释，
     其中的关键字与括号不参与结构分析；
  4. 报告括号不配对（精确到行、列、哪个括号）、字符串未闭合、块注释未结束。

用法：
  python3 code_outline.py <源文件> [--json]
  cat foo.c | python3 code_outline.py          # 从标准输入读取
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

# 定义关键字 -> 定义类别。可按目标语言增删。
DEF_KEYWORDS = {
    "def": "function",
    "function": "function",
    "fn": "function",
    "func": "function",
    "class": "class",
}

OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
CLOSE_TO_OPEN = {v: k for k, v in OPEN_TO_CLOSE.items()}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Token:
    kind: str        # 'ident' | 'bracket' | 'punct'
    text: str
    line: int        # 1 起始行号
    col: int         # 1 起始列号
    start: int       # 源码偏移（含）
    end: int         # 源码偏移（不含）


@dataclass
class Definition:
    kind: str                # 'function' | 'class'
    name: str
    params: str | None       # None 表示无参数列表（如 class）；'' 表示空括号 ()
    start_line: int
    end_line: int
    depth: int = 0
    children: list["Definition"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "params": self.params,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "depth": self.depth,
            "children": [c.to_dict() for c in self.children],
        }


# ---------------------------------------------------------------------------
# 第一遍：词法扫描（多状态状态机）
# 状态：CODE / 行注释 / 块注释 / 单行字符串 / 三引号字符串
# 只产出标识符、括号、少量标点；字符串与注释整体跳过，只做词法错误检查。
# ---------------------------------------------------------------------------

def lex(src: str) -> tuple[list[Token], list[tuple[int, int, str]]]:
    tokens: list[Token] = []
    errors: list[tuple[int, int, str]] = []
    i, n = 0, len(src)
    line, line_start = 1, 0

    def col(pos: int) -> int:
        return pos - line_start + 1

    while i < n:
        ch = src[i]

        if ch == "\n":
            line += 1
            line_start = i + 1
            i += 1
            continue
        if ch in " \t\r\f\v":
            i += 1
            continue

        # --- 行注释：// 或 # ---
        if (ch == "/" and src.startswith("//", i)) or ch == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue

        # --- 块注释：/* ... */ ---
        if ch == "/" and src.startswith("/*", i):
            start_line, start_col = line, col(i)
            i += 2
            closed = False
            while i < n:
                if src[i] == "\n":
                    line += 1
                    line_start = i + 1
                    i += 1
                elif src.startswith("*/", i):
                    i += 2
                    closed = True
                    break
                else:
                    i += 1
            if not closed:
                errors.append((start_line, start_col,
                               f"块注释未结束（自第 {start_line} 行第 {start_col} 列开始）"))
            continue

        # --- 字符串：'...' "..." '''...''' \"\"\"...\"\"\" ---
        if ch in "\"'":
            start_line, start_col = line, col(i)
            if src.startswith(ch * 3, i):
                # 三引号字符串：可跨行，以连续三个相同引号结束
                i += 3
                closed = False
                while i < n:
                    if src[i] == "\\":            # 转义：跳过下一个字符
                        if i + 1 < n and src[i + 1] == "\n":
                            line += 1
                            line_start = i + 2
                        i += 2
                        continue
                    if src.startswith(ch * 3, i):
                        i += 3
                        closed = True
                        break
                    if src[i] == "\n":
                        line += 1
                        line_start = i + 1
                    i += 1
                if not closed:
                    errors.append((start_line, start_col,
                                   f"三引号字符串未闭合（自第 {start_line} 行第 {start_col} 列开始）"))
            else:
                # 单行字符串：不允许跨行（反斜杠续行除外）
                i += 1
                closed = False
                while i < n:
                    c = src[i]
                    if c == "\\":                 # 转义：跳过下一个字符
                        if i + 1 < n and src[i + 1] == "\n":
                            line += 1
                            line_start = i + 2
                        i += 2
                        continue
                    if c == ch:
                        i += 1
                        closed = True
                        break
                    if c == "\n":
                        break                     # 未闭合：停在换行前恢复
                    i += 1
                if not closed:
                    errors.append((start_line, start_col,
                                   f"字符串未闭合（自第 {start_line} 行第 {start_col} 列开始）"))
            continue

        # --- 标识符 / 关键字 ---
        if ch.isalpha() or ch == "_" or ord(ch) > 127:
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_" or ord(src[j]) > 127):
                j += 1
            tokens.append(Token("ident", src[i:j], line, col(i), i, j))
            i = j
            continue

        # --- 数字（跳过，避免 1.5 之类干扰） ---
        if ch.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "._"):
                j += 1
            i = j
            continue

        # --- 括号 ---
        if ch in "()[]{}":
            tokens.append(Token("bracket", ch, line, col(i), i, i + 1))
            i += 1
            continue

        # --- 结构相关标点 ---
        if ch in ";:,":
            tokens.append(Token("punct", ch, line, col(i), i, i + 1))
            i += 1
            continue

        i += 1  # 其余字符（运算符等）与结构无关，跳过

    return tokens, errors


# ---------------------------------------------------------------------------
# 第二遍：括号配对检查（独立 pass，覆盖全部括号 token）
# ---------------------------------------------------------------------------

def check_brackets(tokens: list[Token],
                   errors: list[tuple[int, int, str]]) -> None:
    stack: list[Token] = []
    for tok in tokens:
        if tok.kind != "bracket":
            continue
        if tok.text in OPEN_TO_CLOSE:
            stack.append(tok)
        elif not stack:
            errors.append((tok.line, tok.col, f"括号不配对：多余的 '{tok.text}'"))
        elif stack[-1].text != CLOSE_TO_OPEN[tok.text]:
            top = stack[-1]
            errors.append((tok.line, tok.col,
                           f"括号不配对：'{tok.text}' 与第 {top.line} 行第 {top.col} 列的 "
                           f"'{top.text}' 不匹配"))
            # 恢复策略：不弹栈，继续检查后续括号
        else:
            stack.pop()
    for tok in stack:
        errors.append((tok.line, tok.col,
                       f"括号不配对：'{tok.text}' 未闭合（文件结束时仍无 "
                       f"'{OPEN_TO_CLOSE[tok.text]}'）"))


# ---------------------------------------------------------------------------
# 第三遍：定义提取与嵌套结构
# pending    —— 已看到关键字、正在收集头部（名称/参数）的定义
# open_defs  —— 主体花括号已打开、尚未闭合的定义栈（栈顶即当前内层定义）
# ---------------------------------------------------------------------------

def extract_definitions(src: str, tokens: list[Token],
                        errors: list[tuple[int, int, str]]) -> list[Definition]:
    roots: list[Definition] = []
    open_defs: list[tuple[Definition, int]] = []  # (定义, 主体 { 的花括号深度)
    pending: Definition | None = None
    curly = 0
    i, n = 0, len(tokens)

    def attach(defn: Definition) -> None:
        defn.depth = len(open_defs)
        if open_defs:
            open_defs[-1][0].children.append(defn)
        else:
            roots.append(defn)

    def finalize_header_only(defn: Definition) -> None:
        """头部后没有 { 主体（如前置声明、无花括号语法）：起止行相同。"""
        if not defn.name:
            errors.append((defn.start_line, 1, "定义缺少名称，已忽略"))
            return
        defn.end_line = defn.start_line
        attach(defn)

    while i < n:
        tok = tokens[i]

        # 1) 定义关键字：开启新的 pending
        if tok.kind == "ident" and tok.text in DEF_KEYWORDS:
            if pending is not None:
                finalize_header_only(pending)
            pending = Definition(DEF_KEYWORDS[tok.text], "", None, tok.line, tok.line)
            i += 1
            continue

        # 2) 关键字后的第一个标识符是名称
        if pending is not None and not pending.name and tok.kind == "ident":
            pending.name = tok.text
            i += 1
            continue

        # 3) 名称后的 '(' 开始参数列表：按嵌套深度找到匹配的 ')'
        if pending is not None and pending.name and tok.text == "(":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                t = tokens[j]
                if t.kind == "bracket":
                    if t.text in OPEN_TO_CLOSE:
                        depth += 1
                    else:
                        depth -= 1
                j += 1
            raw = src[tok.end:tokens[j - 1].start] if depth == 0 else src[tok.end:]
            pending.params = " ".join(raw.split())  # 空白归一化，便于单行展示
            i = j
            continue

        # 4) '{'：若 pending 已有名称，则主体开始，定义入栈
        if tok.text == "{":
            curly += 1
            if pending is not None:
                if pending.name:
                    attach(pending)
                    open_defs.append((pending, curly))
                else:
                    errors.append((pending.start_line, 1, "定义缺少名称，已忽略"))
                pending = None
            i += 1
            continue

        # 5) '}'：关闭所有主体深度 >= 当前深度的定义
        if tok.text == "}":
            while open_defs and open_defs[-1][1] >= curly:
                defn, _ = open_defs.pop()
                defn.end_line = tok.line
            curly = max(0, curly - 1)
            i += 1
            continue

        # 6) ';'：前置声明（无主体），丢弃 pending
        if tok.text == ";" and pending is not None:
            pending = None
            i += 1
            continue

        i += 1

    # 文件结束：收尾未决的 pending 与未闭合的主体
    if pending is not None:
        finalize_header_only(pending)
    last_line = tokens[-1].line if tokens else 1
    while open_defs:
        defn, _ = open_defs.pop()
        defn.end_line = last_line

    return roots


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def flatten(roots: list[Definition]) -> list[Definition]:
    out: list[Definition] = []

    def walk(d: Definition) -> None:
        out.append(d)
        for c in d.children:
            walk(c)

    for r in roots:
        walk(r)
    return out


def format_params(params: str | None) -> str:
    return "" if params is None else f"({params})"


def print_report(roots: list[Definition],
                 errors: list[tuple[int, int, str]],
                 out=sys.stdout) -> None:
    defs = flatten(roots)
    print("=== 定义清单 ===", file=out)
    if not defs:
        print("（未发现任何定义）", file=out)
    for idx, d in enumerate(defs, 1):
        params = format_params(d.params)
        print(f"[{idx}] {d.kind:<8} {d.name}{params}  "
              f"行 {d.start_line}-{d.end_line}  嵌套层级 {d.depth}", file=out)

    print("\n=== 嵌套结构 ===", file=out)

    def walk(d: Definition) -> None:
        params = format_params(d.params)
        print(f"{'  ' * d.depth}{d.kind} {d.name}{params}  "
              f"[行 {d.start_line}-{d.end_line}]", file=out)
        for c in d.children:
            walk(c)

    if not roots:
        print("（空）", file=out)
    for r in roots:
        walk(r)

    print("\n=== 词法/结构错误 ===", file=out)
    if not errors:
        print("（无错误）", file=out)
    for line, col, msg in sorted(errors):
        print(f"第 {line} 行第 {col} 列：{msg}", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纯标准库源码结构提取器")
    parser.add_argument("path", nargs="?", help="源文件路径（缺省读标准输入）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    if args.path:
        with open(args.path, "r", encoding="utf-8") as f:
            src = f.read()
    else:
        src = sys.stdin.read()

    tokens, errors = lex(src)
    check_brackets(tokens, errors)
    roots = extract_definitions(src, tokens, errors)

    if args.json:
        json.dump({
            "definitions": [d.to_dict() for d in roots],
            "errors": [
                {"line": line, "col": col, "message": msg}
                for line, col, msg in sorted(errors)
            ],
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_report(roots, errors)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

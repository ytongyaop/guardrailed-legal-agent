/**
 * 文案口径（与 `scripts/check_ui_text.py` 同一套规则）：简体、标点统一。
 *
 * 只扫 `web/src` 下的源码与模板，跳过注释行；发现违规就报出文件与行号。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, expect, it } from 'vitest'

// vitest 的 `import.meta.url` 不是 file: 协议，统一用工作目录（= web/）定位源码
const SRC = join(process.cwd(), 'src')
const SOURCE_EXTS = ['.vue', '.ts', '.css']
const CJK = '[\\u4e00-\\u9fff]'
const SKIP_FILES = ['components.d.ts']

const CJK_PATTERN = /[\u4e00-\u9fff]/

const RULES: readonly (readonly [string, RegExp])[] = [
  ['替换字符', /\ufffd/],
  ['全角空格', /\u3000/],
  ['直角引号', /[「」『』]/],
  ['emoji 勾叉', /[✅❌✓✗]/],
  ['单个省略号', /(?<!\u2026)\u2026(?!\u2026)/],
  ['中文间半角逗号', new RegExp(`${CJK},(?=${CJK})`)],
  ['中文间半角分号', new RegExp(`${CJK};(?=${CJK})`)],
  ['中文间半角冒号', new RegExp(`${CJK}:(?=${CJK})`)],
  ['中文后半角括号', new RegExp(`${CJK}[()]`)],
  ['中文旁竖线', new RegExp(`${CJK}\\s*[|｜]\\s*(?=${CJK})`)],
  ['连续英文句点', /\.\.\./],
]

function walk(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      files.push(...walk(full))
      continue
    }
    if (SKIP_FILES.includes(entry)) continue
    if (SOURCE_EXTS.some((ext) => entry.endsWith(ext))) files.push(full)
  }
  return files
}

function isComment(line: string): boolean {
  const trimmed = line.trim()
  return (
    trimmed.startsWith('//') ||
    trimmed.startsWith('*') ||
    trimmed.startsWith('/*') ||
    trimmed.startsWith('<!--')
  )
}

describe('界面文案口径', () => {
  it('源码与模板里没有简体/标点违规', () => {
    const problems: string[] = []
    for (const file of walk(SRC)) {
      const text = readFileSync(file, 'utf8')
      text.split(/\r?\n/).forEach((line, index) => {
        if (isComment(line)) return
        const hasCjk = CJK_PATTERN.test(line)
        for (const [label, pattern] of RULES) {
          // 「连续英文句点」只针对中文文案：JS 展开运算符 `...` 不是文案问题
          if (label === '连续英文句点' && !hasCjk) continue
          if (pattern.test(line)) {
            problems.push(`${relative(SRC, file)}:${index + 1} [${label}] ${line.trim().slice(0, 80)}`)
          }
        }
      })
    }

    expect(problems).toEqual([])
  })
})

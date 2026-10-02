# Tepat — Pemeriksa Kewujudan Kata Bahasa Melayu

马来语词存在性检查器：**把可疑的词高亮出来让人调查，不替人下结论**。

逻辑源自 AnnAgent 的 mech_flags 机械层（零频词检测 + ms/id 后缀规则 + 词缀剥离），
外包成独立桌面工具：`tepat.exe`（本地服务）+ Chrome 扩展（右键检查）。

## 组成

```
tepat.exe（双击即用，~48MB 单文件）
 ├─ 848k 本地词表（puzzle 语料 words 表）
 ├─ 5.2M bigram 表（句子冷密度，c≥2）
 ├─ 17 条语法规则正则（Anna rule_book 同源）+ 329 条错误搭配 blacklist
 ├─ PRPM 在线词典查询（本地 sqlite 缓存 + 1 词/秒限流）
 └─ 系统托盘（左键/右键菜单：Buka UI / Keluar）

extension/（Chrome 扩展，可选）
 └─ 选中文字 → 右键 → Tepat 菜单：
     📖 Semak PRPM / ✓ Semak kewujudan kata / ✗ Raise Error
     raise 的错误存本地日志，popup 可导出 JSON
```

## 使用

1. 双击 `tepat.exe` → 托盘出现图标（服务在 127.0.0.1:8377）
2. 右键托盘 → **Buka UI** → 网页版检查器（贴文本检查）
3. 或安装 Chrome 扩展（`chrome://extensions` → Load unpacked → 选 `extension/`），
   在任意网页选中马来文右键检查
4. **Keluar** 完全退出

单实例：已在运行时再次双击不会产生第二个实例/托盘。

## 检查层级（置信度浅→深）

| 层 | 判据 | 标记 |
|---|---|---|
| blacklist bigram | 人工确认的错误搭配 | 🔴 深 |
| 语法规则 high | 结构性错误（双 pemeri、yang mana） | 🔴 深 |
| ms/id 后缀规律 | mengerahken→mengerahkan 型 | 🔴 深+建议 |
| 零频词 | 语料 848k 词表查无 | 🟠 中 → 可选 PRPM 三态 |
| 语法规则 medium/low | 上下文相关/宽网提醒 | 🟠/🟡 |
| 句子冷密度 | bigram 支持率 <40% 的句子 | 🟡 浅 |

PRPM 三态语义：✓ 词典有 / ✗ 无词条 / ? 不可达（**未验证 ≠ 拼错**）。

## 已知边界

- 只查"词是否存在"，不查"用得对不对"（deraf 型真词误用不在能力范围）
- 语料含英语词（BM 现实文本夹英文是常态，策略是放行）
- Hansard 议会语料的 OCR 错词经编辑距离过滤，长尾残留已知
- PRPM 查询需要网络；离线时词表层照常工作

## 构建

```
build.bat        # PyInstaller onefile + 词表/bigram/规则/图标
```

词表再生成：`puzzle/ngrams2.db` 的 `words` 表导出（见 AnnAgent 主仓）。

## 数据与规则维护

- `rules.json` — 语法规则/后缀映射/词缀表（**外置热改**：改完重启 exe 生效，无需重打包）
- `blacklist.json` — 错误搭配表（同上）
- 印尼特有词黑名单与用户 raise 闭环见 AnnAgent 主仓 `puzzle/indo_blacklist.md`

## License

内部工具。PRPM 词典数据来源：DBP（prpm.dbp.gov.my），仅作查询引用。

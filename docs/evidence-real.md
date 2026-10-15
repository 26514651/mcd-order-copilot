# 真实调用证据

> 本文记录本项目对麦当劳 MCP 服务（`https://mcp.mcd.cn`）的**真实调用**。
> 所有数据由 `scripts/sync_fixtures.py` 生成，可自行复跑验证。
> 调用时间：**2026-10-15 15:17（CST）**
> 样本门店：**麦当劳广州芳村立白中心餐厅**（`storeCode=1400886`）

---

## 一、连接与能力发现

### 1.1 协议握手

```
POST https://mcp.mcd.cn
Content-Type: application/json
Accept: application/json, text/event-stream
Authorization: Bearer $MCD_MCP_TOKEN

{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-03-26","capabilities":{},
           "clientInfo":{"name":"mcd-order-copilot","version":"0.1.0"}}}
```

响应：

```json
{"jsonrpc":"2.0","id":1,"result":{
  "capabilities":{"tools":{}},
  "protocolVersion":"2025-03-26",
  "serverInfo":{"name":"mcd-mcp","version":"1.0.0"}}}
```

HTTP 200 · 传输方式：Streamable HTTP（JSON-RPC 2.0）

### 1.2 工具清单

`tools/list` 返回 **35 个工具**。

> ⚠️ 网上流传的"37 个""33 个"都与实测不符。本项目以 `tools/call` 的实测结果为准，计数方法见 `scripts/sync_fixtures.py`。

---

## 二、实际调用的 4 个工具（全部只读）

| # | 工具 | 参数 | 结果 |
|---|---|---|---|
| 1 | `query-nearby-stores` | `{"beType":1,"searchType":2,"keyword":"麦当劳","city":"广州"}` | 返回 5 家门店 |
| 2 | `list-nutrition-foods` | `{}` | **160 条**营养数据 |
| 3 | `query-meals` | `{"storeCode":"1400886","orderType":1,"beType":1}` | **115 项** / **15 个分类** |
| 4 | `query-meal-detail` | `{"storeCode":"1400886","orderType":1,"beType":1,"code":"<code>"}` | **115 次调用，0 失败**，全部返回 `rounds` 结构 |

**本项目不调用任何写操作工具**（`create-order` / `cancel-order` / `mall-create-order` / `draw-lottery` / 积分兑换等一律未触碰）。

---

## 三、关键发现：接口不返回纯 JSON

这是复现时最容易踩的坑，记录下来：

`tools/call` 的 `content[].text` **不是**裸 JSON，而是**「字段说明 + 原始 payload」**：

```
每个门店的基本信息按单行展示：
- **门店名称**: data[].storeName
- **门店地址**: data[].address
...
{"success":true,"code":200,"message":"请求成功","data":[...]}
```

因此 `json.loads(text)` **必然失败**。本项目用 `extract_json_payload()` 扫描每一个 `{` 起点、`raw_decode` 出最大的合法对象（实现见 `src/mcd_order_copilot/mcp_client.py`）。

### 另一个坑：营养表是 CSV 字符串，不是 JSON 数组

`list-nutrition-foods` 的 `data` 字段是一个**字符串**：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}
猪柳麦满分,null,1288,308,16,16,24,781,213
巨无霸,null,2146,513,27,26,42,961,171
...
```

首行是方括号包裹的表头，其后每行一条。解析器见 `parse_nutrition_rows()`。

---

## 四、实测数据样本（可直接对照官方接口核验）

### 4.1 营养值（官方实时）

| 餐品 | 能量 (kcal) | 蛋白 (g) | 脂肪 (g) | 碳水 (g) | 钠 (mg) | 钙 (mg) |
|---|---:|---:|---:|---:|---:|---:|
| 巨无霸 | **513** | 27 | 26 | 42 | 961 | 171 |
| 板烧鸡腿堡 | 391 | — | — | — | — | — |
| 麦辣鸡腿汉堡 | 485 | — | — | — | — | — |
| 中薯条 | 289 | — | — | — | — | — |
| 猪柳麦满分 | 308 | 16 | 16 | 24 | 781 | 213 |

> 表中「—」处为本文截取展示，完整 6 项在 `fixtures/nutrition.json`（160 条）中。

**第三方攻略给出的巨无霸热量是 503 / 530 / 602 —— 三个都不一样，且都无时效标注。**

### 4.2 套餐结构（`rounds`，可直接算出总营养）

以「麦辣鸡腿汉堡三件套」为例，`query-meal-detail` 返回 3 轮：

```
轮次[1] 麦辣鸡腿汉堡        麦辣鸡腿汉堡 485 kcal
轮次[2] 选择套餐内小食      中薯条 289 / 大薯条 379 / 小杯玉米杯 53 / 大杯玉米杯 87 …
轮次[3] 选择套餐内饮料      可乐中杯 147 / 无糖可乐中杯 0 / 雪碧中杯 105 … 共 23 个选项
```

每个 choice 携带：`code`、`name`、`isDefault`、`diffPrice`、`supportModify`。

**推论：套餐营养 = 各选中成分之和。这是可算的**——本项目据此实现了"换配餐实时重算"（见 `index.html` 详情面板）。

### 4.3 特制项（可去掉的配料）

以「巨无霸」为例（`supportModify=true`）：

| 可去掉的配料 | code |
|---|---|
| 巨无霸酱 | 100116 |
| 吉士 | 100141 |
| 生菜 | 100168 |
| 酸黄瓜 | 100202 |
| 洋葱粒 | 100212 |

> ⚠️ **但接口不返回"去掉后少多少 kcal"，营养表里也没有单独的酱料条目。**
> 因此本项目**只列清单，不做热量差值**。任何"去酱省 X 千卡"的说法都缺少官方来源，本项目不采用。

### 4.4 图片

`query-meals` 返回的 `image` 字段指向官方 CDN，实测全部可下载：

| 餐品 | URL 片段 | 大小 |
|---|---|---|
| 巨无霸（600px） | `menu-img.mcd.cn/pcm/prod/menu/20250611/product/MS_1100_600_...png` | 73,678 B |
| 板烧鸡腿堡 | `menu-img.mcd.cn/pcm/prod/menu/ProductPool/product/MS_1406_320.png` | 39,568 B |

共拉取 **115 张**，存放于 `docs/images/`，由 `scripts/fetch_images.py` 复现。

---

## 五、如何自行复现

```bash
# 1. 申请 Token（免费）
#    https://open.mcd.cn/mcp → 手机号登录 → 控制台 → 激活
export MCD_MCP_TOKEN="你的Token"        # Windows: set MCD_MCP_TOKEN=...

# 2. 重新拉取全部 fixtures（含 115 次详情调用，约 40 秒）
python scripts/sync_fixtures.py --city 广州 --details all

# 3. 重新下载产品图
python scripts/fetch_images.py

# 4. 重新生成演示页
python scripts/build_demo.py
```

全部脚本仅使用 Python 标准库，**无需安装任何依赖**。

---

## 六、能力缺口（诚实清单）

复现时会遇到的官方数据边界，本项目全部如实呈现、不做估算：

| # | 缺口 | 影响 | 本项目处理 |
|---|---|---|---|
| 1 | **无 GI 值（升糖指数）** | 只能做碳水总量，做不了升糖控制 | 标签用「控碳水」，禁用"控糖推荐" |
| 2 | **无配料级营养** | 拿不到"去酱后热量" | 只列可去清单 + 同类 SKU 对照 |
| 3 | **部分新品无营养数据** | 如龙焰系列、部分麦旋风 | 显示「官方暂无」，不参与筛选、不估算 |
| 4 | **无过敏原数据** | 过敏人群无法服务 | 明确列为不服务人群 |
| 5 | `nutritionDescription` 全为 `null` | 无法用官方说明做卖点 | 忽略该字段 |
| 6 | 营养表未标注单位口径 | 可能被误读为每 100g | 界面统一标注「每份」 |
| 7 | `query-meals` 必须传 `storeCode` | 无法"先看菜单再选店" | 入场第一步即为门店定位（自动完成） |

---

*本文件由实测生成，内容与 `fixtures/_meta.json` 一致。任何数字均可在官方接口复核。*

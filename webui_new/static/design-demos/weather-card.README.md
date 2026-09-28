# 天气卡片改版 · 堆叠长条

出发前确认里「天气与随行准备」的排版改版：一天一条长条，按天堆叠，收起时只露脊线，展开后让出昼夜详情。今天那条常驻不折叠，建议和随行准备都在里面。

## 预览

在仓库根目录运行：

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory webui_new
```

打开 http://127.0.0.1:8765/static/design-demos/weather-card.html 。
现有服务运行时，也可直接访问 `/static/design-demos/weather-card.html`。

页面并排三种行程长度（4 天 / 3 天 / 2 天，后者带短建议用于验证不出现多余展开入口），可切浅色深色。调试参数：`?theme=dark`、`?expand=1`。

## 改了什么

改版前是一列「标签在上、值在下」的等权小字（`天气 / 晴`、`温度 / 18–26°C`、`体感 / …`），没有主次，长建议直接铺一整段，且只有一天。

改版后：

- **一天一条长条。** 脊线是 `图标 · 今天 9/18 · 晴 · 31–38°C`，收起时露出的就是这一行。今天那条脊线温度放大到 21px 做锚点，其余保持 12.5px。
- **收起时堆叠。** 后续几条压到脊线高度，上一条压在下一条上（`margin-top: -4px`，只吃掉脊线 11px 上内边距的一部分，图形和文字都不会被切），并按层往里收（`scaleX(1 - depth * .026)`），投影落在下一条顶边上。收拢/摊开时按层错开 32ms，从下往上收、从上往下摊。
- **展开后是昼夜详情。** 每条让出一行 `白天 多云 26°C · 夜间 雷阵雨 18°C · 东南风 3 级`。
- **图标和色调跟着天气走。** 命中「雷/雨」→ 雨、「雪/冰」→ 雪、「晴」→ 晴，其余（阴/多云/雾霾）→ 云；含「转」时取转折后的那段，`多云转晴` 显示晴、`晴转多云` 显示云。每条按自己的天气取 `--weather-tint`（晴 = 琥珀，雨 = 石蓝，雪 = 灰蓝，阴 = 中性），用在图标底色和右上角那层极淡的径向渐变上。
- **建议默认收在两行。** 没被截断就不显示展开开关，短文案不会出现点了没反应的按钮。

## 数据边界（这是主要成本）

`DepartureWeather`（`core/presentation/answer_document.py:87`）是**单天**的：`condition / temperature / humidity / advice / preparation`。这里需要改成多天。

好消息是上游数据已经有了：`amap.py` 已经请求了 `extensions=all`，`WeatherReport.forecasts` 里最多 4 天，字段是 `date / day_condition / night_condition / low_c / high_c / day_wind / day_power`。**长条里的日期、白天/夜间天气、温度区间、风都来自这里，不是 LLM 写的**，只是目前没有从 `WeatherReport` 流到 `DepartureWeather`。

需要新增的只有两处：

1. 给 `PreDepartureChecklist` 加一个预报数组字段（如 `forecast: list[WeatherForecastDay]`），或把 `weather` 改成列表。前者改动小，建议选它。
2. 脊线用的合成天气文案（`多云转雷阵雨`）和今天的 `advice / preparation` 仍是 LLM 产出的，需要在提示词里跟着调。今天以外各天的建议目前**没有**数据来源，所以 demo 里只展示昼夜详情，没有编造。

demo 的 `SAMPLES` 是按 `WeatherForecastDay` 的字段形状写死的样例，日期在浏览器里从今天推算，`今天 / 明天 / 后天 / 周X` 也是算出来的。

## 落地方式

`weather-card.js` 的 `renderWeatherPanel()` / `renderDetail()` / `setupDeck()` / `renderAdvice()` 准备替换 `webui_new/static/answer-card.js:293` 的 `renderDepartureWeather()`；`GLYPHS` 和 `weatherFamily()` 是新增的。CSS 里 `.departure-*` 替换 `webui_new/static/answer-card.css:400-408` 的旧样式，`.wd-*` 只是 demo 页外壳，不用带过去。

折叠的两个量在展开态下量完写回 `--peek`（脊线高）和 `--strip-h`（整条高），CSS 只用 `max-height` 过渡——两端都是确定值才能过渡，`auto` 不可动画。窗口尺寸变化时 `measure()` 重算。

`GLYPHS` 里的 SVG 用 `innerHTML` 写入，字符串是写死的常量，不接用户输入。

## 验证

Chrome headless 截图核对：浅色三种行程长度（收起）、深色（`?theme=dark`）、展开态（`?expand=1`）、窄屏单列。构建过程中修掉两个 bug：折叠最初用负 margin 让上一条盖住下一条，方向是反的（盖住的是脊线、露出来的是详情），改成 `max-height` 折叠；折叠开关的文案节点漏了创建，`forEach` 在第一个样本就抛错，只剩一个卡片、栅格因此塌成单列。

窄屏截图有一点要留意：Chrome headless 的窗口宽度有下限（约 500px），`--window-size=390` 会被夹住而截图仍按 390 裁切，看起来像布局溢出。真正有效的窄屏检查是 520px。另外首屏有一次收拢动画，截图取态要留够时间（`--virtual-time-budget` 给到 6000）。

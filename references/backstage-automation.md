# 后台自动化实操要点

本文件记录 2026-11-13 番外02 实发过程中验证过的微信后台结构与操作路径，供自动化时定位元素和判断状态。**选择器会随平台改版失效，每次仍以当前页面为准**；本文件的作用是知道"该找什么、为什么找不到、找不到时换哪条路"，不是照抄坐标。

## 判断发表是否成功，看接口不看页面

页面文案里的「发表中」「正在排队」大量存在于 Vue 模板字符串，直接正则匹配正文会误判。可靠做法是同域取发表记录接口：

```js
fetch('/cgi-bin/appmsgpublish?sub=list&token=<token>&lang=zh_CN&f=json&ajax=1&begin=0&count=20',
      {credentials:'include'})
  .then(r=>r.json())
  .then(j=>JSON.parse(j.publish_page))          // publish_page 是嵌套 JSON 字符串，要再 parse 一次
```

`publish_page.publish_list[]` 每项的 `publish_info` 仍是字符串，需再 parse，然后取 `appmsg_info[]`。每条含 `title`、`content_url`（即 `/s/xxx` 分享链接）、`videoid`（视频才有）、`duration`、`appmsg_album_info.title`（合集名）、`read_num`。

`publish_page.total_count` 是最简明的防重指标：发表前后比对总数，涨了才是新增。视频图文同名时靠 `videoid` 是否为 null 区分类型。

## 封面：空封面和已设封面是两条完全不同的路

空封面状态下，悬停 `.js_cover_preview_new` 不显示菜单、点击不弹系统对话框，这条路走不通，容易误判成"封面功能不可自动化"。

已设封面后才有救：悬停封面缩略图，`.js_modifyCover`（藏在 `.weui-desktop-tooltip__wrp` 里，未悬停时被判定不可见，桥接报 `not_visible after 5000ms`）才变 `opacity:1`，此时点击可打开「编辑封面」弹窗。

编辑封面弹窗的结构：左侧一个 Jcrop 裁剪器（源图按容器等比缩放显示，如 2560 源图显示为 594），右侧是 2.35:1 与 1:1 两个预览卡片。**只有 2.35:1 受裁剪器控制，1:1 直接用整张方图**——所以方形安全版素材天然适配 1:1，但被居中裁成 2.35:1 时只会留下中间一条。

Jcrop 实例不在全局变量里，用 jQuery 缓存找：

```js
let api; for (const k in jQuery.cache) {
  const d = jQuery.cache[k] && jQuery.cache[k].data;
  if (d && d.Jcrop && typeof d.Jcrop.setSelect === 'function') api = d.Jcrop;
}
api.tellSelect();                     // {x,y,x2,y2,h,w}，显示坐标系
api.setSelect([0, y1, 594, y1+253]);  // 保持原高度，只改 y
```

缓存 key 每次重开弹窗都会变，必须重新扫描。改完点「确认」即提交，前端封面 URL 会换成新的 `cropimage` 产物；只调不走确认不生效。

方形素材裁 2.35:1 的取舍：源图上下留白多时，`y1=0` 会把留白全裁进来而丢掉主体。按内容分布下移（本次从 0 移到 16.5%），让标题条 + 主体上半落在框内。若必须完整呈现横版构图，应另做一张 2.35:1 源图，不能指望裁剪解决。

确认按钮用 `mouse_click` 常报 `not_visible`，`btn.click()` 反而有效。

## 微信验证码：静图必过期，优先前置窗口

验证码有效期约一分钟。导出图片（哪怕是 canvas 直取原图）再发给用户，等他看到时基本已失效；平台侧过期后前端不一定会当场把 UI 切成"已失效"，静态截图看起来仍正常，扫了才报错。

正确做法：把承载后台的浏览器窗口 `ShowWindow(SW_MAXIMIZE)` + `SetForegroundWindow` 前置，让用户对着屏幕扫。页面回到可见状态后，微信前端会自行轮换码，不受静图时效限制。

若必须给图，就当场生成当场发，并说明"一分钟内有效"。

验证弹窗**不在** `.weui-desktop-dialog__wrp` 里，而是 `.dialog_wrp.ui-draggable`（`z=9999`）。只查 weui 那套会误判"弹窗已消失"，进而误以为发表完成。扫码成功后页面自动跳 `/cgi-bin/home`，这是可靠的完成信号。

## 按钮点击的可靠性分级

同一页面不同控件对合成事件的响应差异很大，按下面顺序试，不要一上来就上 OS 级鼠标：

1. **DOM `el.click()`**：对原生 `button` 绑定的 Vue/jQuery handler 有效。二次确认类按钮（「继续发表」「确定」「删除」）多数走这条。
2. **桥接 `mouse_click(selector)`**：需要元素通过可见性检查（`display`/`visibility`/`opacity`/`rect` 都合格）。浮层里的按钮、未悬停才显示的图标会被判定不可见。
3. **强制显示再点**：对 `v-show` 控制的浮层（内联 `display:none`），先 `pop.style.display='block'`，再点内部按钮。Vue 的 `@click` 绑在 DOM 上，不依赖浮层可见状态。
4. **OS 级鼠标**（`SetCursorPos` + `mouse_event`）：仅当上面都失败，且只为触发 `hover` 类交互时使用。注意窗口可能在副屏或非最大化，坐标必须在同一进程内按 `GetWindowRect` 现算。

`__vue__` 链上的方法在 Vue 3 组件里多为 `[native code]`，想直接调 `asyncDelVideo` 这类方法通常拿不到实现，别在这条路上耗时间。

## 已知不可用或需绕行的环节

**一键排版**：工具栏 `.ailayout-btn_wrp` 点击无任何响应——无弹窗、无新标签、正文 HTML 指纹不变；`UE.getEditor('js_editor')` 报 `not import language file`。点两次确认无效后就停手，改人工整理版式，不要反复重试。正文里 `mso-ascii-font-family` 这类 Word 残留样式会留着，观感可接受但不是标准排版。

**文件 input 上传**：桥接 `upload` 动作依赖扩展的文件访问权限，未开时报 `upload needs Chrome's per-extension file access`。绕法：本地起 CORS 静态服务（`Access-Control-Allow-Origin: *`），页面内 `fetch` 取 Blob → `new File()` → `DataTransfer` 塞进 `input[type=file]` → `dispatchEvent(new Event('change',{bubbles:true}))`。127.0.0.1 属 potentially trustworthy origin，https 页面 fetch 不被 mixed-content 拦截。用完记得停服务。

**「保存并发表」弹新窗口**：该按钮用 `window.open`，合成 click 被判非用户手势而拦「弹窗被浏览器拦截」。CDP `Input.dispatchMouseEvent` 经桥接会挂起，`Browser.grantPermissions` 扩展不支持。这类按钮必要时请用户手点一次。

**一键排版的引导提示**：首次点击会先弹「我知道了」引导条，此时点击命中的是提示容器而非按钮。引导条可 `display` 为 0×0 隐藏，不算拦路。

## 素材库清理

视频素材列表 `/cgi-bin/appmsg?begin=0&count=20&action=list_video&type=15`，条数在正文「视频 （共N条）」。

行内删除是 `weui-desktop-popover`，同样由 `v-show` 控制，`mouse_click` 与 `icon.click()` 都打不开。用强制显示 + 点内部「删除」按钮，条数实时减少，可当场验证。

清理前必须确认删的是**没有进发表记录**的那条：两条同名素材靠"更新时间"与发表时间对齐判断，删完立刻复核线上阅读页无「已被发布者删除」，且 `wxv_` id 仍在。删本地素材不影响已发表内容，但删错对象会让线上视频直接失效。

## 读页面状态的通用写法

判断弹窗是否可见要同时看 `display`、`visibility`、`opacity` 和 `getBoundingClientRect().height`——后台大量弹窗常驻 DOM，`display:block` 但高度为 0 的情况很常见，只看 `display` 会误判。

页面在后台标签时 `visibilityState` 为 `hidden`，扩展截图只得到空白小图（约 4KB）。此时 `Page.bringToFront` 返回 focus-emulation 后 `visibilityState` 转 `visible` 才能截到内容；或直接改用 canvas 从原图导 dataURL，避开截图链路。

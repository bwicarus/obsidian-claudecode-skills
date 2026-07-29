/* offscreen.js — 旧 Pi 电脑语音桥的兼容占位。
 *
 * 电脑语音现由普通网页和书籍 PWA 共同加载的 rc-computer-voice 直连运行时
 * 负责。这个保留文件故意不读取、迁移或删除旧 storage 记录，也不连接
 * Native Messaging、不访问 Pi、不创建媒体轨。旧 offscreen 页面即使因浏览器
 * 更新竞态短暂残留，也不会恢复历史配对或启动采音。
 */

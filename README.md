# AutoPhigros

基于frida与[phisap](https://github.com/kvarenzn/phisap/tree/dev)实现的全自动游玩Phigros.

基本是DeepSeek-v4.1 flash写的,借用了phisap的规划算法.

## 导航

| 想知道什么                             | 看哪儿                                                                |
|----------------------------------------|-----------------------------------------------------------------------|
| 这是什么东西                           | 往下看                                                                |
| 为什么这么设计、判定怎么算、踩过哪些坑 | [impl.md](docs/impl.md)                                               |
| 如何使用                               | [usage.md](docs/usage.md)                                             |
| 游戏内部：函数地址、字段偏移、伪代码   | [Phigros4.0-音游内核逆向报告.md](docs/Phigros4.0-音游内核逆向报告.md) |

## 设计思路

用frida抓取谱面数据,回传电脑进行规划,卡死Unity主线程直到规划完成.再通过frida精确同步时间轴,完成点击事件列表的播放.

## 杂谈

竞品怎么都不公开啊,是不想公开吗? ^ ^

## LICENSE

AGPL-3.0
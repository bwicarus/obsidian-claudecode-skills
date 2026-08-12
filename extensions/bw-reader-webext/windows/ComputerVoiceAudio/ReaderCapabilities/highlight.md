# 高亮输出

高亮作用于 App/扩展当前稳定选区，使用现有 `highlight.save` 动作：

```text
BWREADER/1 highlight {"color":"yellow","note":null}
BWREADER/1 highlight {"color":"green","note":"可选备注"}
```

颜色只允许 `yellow`、`green`、`blue`、`pink`。没有当前选区或当前宿主不支持高亮时
必须返回失败，不能猜测范围，也不能改成整页标记。

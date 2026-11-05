# 第三方组件许可声明（THIRD-PARTY NOTICES）

ZCBOT 本体以 MIT OR Apache-2.0 双协议发布（见 `LICENSE` 与 `LICENSE-APACHE`）。
除此之外，发布产物中还**打包**了下列第三方组件。它们不是运行时依赖，
而是经 Vite 构建后内联进 `core_plugins/webui/web/js/` 的静态资源，
因此各组件自己的版权声明必须随副本一同分发（MIT 的硬性要求）。

| 组件 | 版本 | 许可 | 出现在 |
|------|------|------|--------|
| element-plus | 2.14.4 | MIT | `js/element-plus.<hash>.js` |
| @element-plus/icons-vue | 2.3.2 | MIT | `js/element-plus.<hash>.js` |
| vue | 3.5.41 | MIT | `js/vue-vendor.<hash>.js` |
| vue-router | 4.6.4 | MIT | `js/vue-vendor.<hash>.js` |

构建时的压缩（Terser `format.comments: false`）与混淆会删除源文件里的
版权注释，所以本文件是这些声明在发布产物中的**唯一载体**：
`core_plugins/webui/web/THIRD-PARTY-NOTICES.txt` 是同一份内容的纯文本副本，
由 `tools/check_webui_artifact.py` 在 CI 中校验其存在性，防止重建时丢失。

以下为各组件随附的许可原文。

### Element Plus

- 包名：`element-plus`
- 版本：`2.14.4`
- 许可：MIT
- 上游仓库：git+https://github.com/element-plus/element-plus.git

```text
MIT License

Copyright (c) 2020-PRESENT Element Plus

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Vue.js

- 包名：`vue`
- 版本：`3.5.41`
- 许可：MIT
- 上游仓库：git+https://github.com/vuejs/core.git

```text
The MIT License (MIT)

Copyright (c) 2018-present, Yuxi (Evan) You

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### Vue Router

- 包名：`vue-router`
- 版本：`4.6.4`
- 许可：MIT
- 上游仓库：git+https://github.com/vuejs/router.git

```text
The MIT License (MIT)

Copyright (c) 2019-present Eduardo San Martin Morote

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Element Plus Icons

- 包名：`@element-plus/icons-vue`
- 版本：`2.3.2`
- 许可：MIT
- 上游仓库：git+https://github.com/element-plus/element-plus-icons.git

```text
The MIT License (MIT)

Copyright (c) 2020-PRESENT Element Plus (https://github.com/element-plus)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

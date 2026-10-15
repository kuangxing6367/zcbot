// 插件清单：每个插件目录可放 plugin.json，声明 id/命令/事件类型/依赖/权限。
// 内核据此校验、注册事件与命令前缀、构建命令级权限表。无清单时回退到"目录即插件"。
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type PluginCommand struct {
	Name string `json:"name"` // 命令前缀，如 /ping
	Perm string `json:"perm"` // 权限等级：空=公开，admin=仅管理员
	Desc string `json:"desc"`
}

type PluginManifest struct {
	ID          string          `json:"id"`
	Name        string          `json:"name"`
	Author      string          `json:"author"`
	Version     string          `json:"version"`
	Description string          `json:"description"`
	Deps        []string        `json:"deps"`
	EventTypes  []string        `json:"event_types"`
	Commands    []PluginCommand `json:"commands"`
}

// LoadManifest 读取插件目录的 plugin.json；不存在返回 nil（回退默认）。
func LoadManifest(dir string) (*PluginManifest, error) {
	p := filepath.Join(dir, "plugin.json")
	b, err := os.ReadFile(p)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var m PluginManifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// Validate 校验清单与目录名一致（防止 id 冲突）。
func (m *PluginManifest) Validate(dirName string) error {
	if m.ID == "" {
		m.ID = dirName
	}
	return nil
}

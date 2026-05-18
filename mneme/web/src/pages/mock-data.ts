export interface DocNode {
  name: string
  type: 'folder' | 'file'
  lang: string
  children?: DocNode[]
  content?: string
}

const AUTH_TS = `## 函数: authenticate

\`\`\`typescript
function authenticate(token: string): AuthResult {
  const isValid = validateToken(token)
  if (!isValid) throw new AuthError(token)
  return { userId: token.userId, role: token.role }
}
\`\`\`

- **输入**: token (string)
- **输出**: AuthResult
- **调用了**: validateToken, AuthError`

const HELPERS_TS = `## 函数: hash

\`\`\`typescript
function hash(input: string): string {
  return crypto.createHash("sha256")
    .update(input)
    .digest("hex")
}
\`\`\`

- **输入**: input (string)
- **输出**: string (hex)
- **被调用方**: auth.authenticate`

const MODAL_TSX = `## 组件: Modal

\`\`\`typescript
interface ModalProps {
  title: string
  visible: boolean
  onClose: () => void
  children: React.ReactNode
}

export function Modal(props: ModalProps) {
  if (!props.visible) return null
  return (
    <div class="modal-overlay">
      <div class="modal-content">
        <h2>{props.title}</h2>
        {props.children}
        <button onClick={props.onClose}>关闭</button>
      </div>
    </div>
  )
}
\`\`\``

const README_MD = `# 项目概述

这是一个 **React 前端项目**，使用 TypeScript 编写。

## 快速开始

\`\`\`bash
npm install
npm run dev
\`\`\`

## 项目结构

- \`src/utils/\` — 工具函数
- \`src/components/\` — UI 组件
- \`docs/\` — 文档

## 注意事项

1. Node.js >= 18
2. npm >= 9`

const API_MD = `## API 设计文档

### 认证接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /auth/login | 用户登录 |
| POST | /auth/logout | 用户登出 |
| GET  | /auth/me | 获取当前用户 |

### Token 管理

系统使用 JWT 进行认证。Token 有效期为 24 小时。

\`\`\`typescript
interface LoginResponse {
  token: string
  user: User
  expiresAt: string
}
\`\`\``

export const mockTree: DocNode[] = [
  {
    name: 'src', type: 'folder', lang: '', children: [
      {
        name: 'utils', type: 'folder', lang: '', children: [
          { name: 'auth.ts', type: 'file', lang: 'typescript', content: AUTH_TS },
          { name: 'helpers.ts', type: 'file', lang: 'typescript', content: HELPERS_TS },
        ],
      },
      {
        name: 'components', type: 'folder', lang: '', children: [
          { name: 'Modal.tsx', type: 'file', lang: 'typescript', content: MODAL_TSX },
        ],
      },
    ],
  },
  {
    name: 'docs', type: 'folder', lang: '', children: [
      { name: 'README.md', type: 'file', lang: 'markdown', content: README_MD },
      { name: 'API.md', type: 'file', lang: 'markdown', content: API_MD },
    ],
  },
]

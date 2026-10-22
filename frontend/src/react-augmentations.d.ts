// Extra attributes on React's built-in types. The import makes this file a module, so the
// declaration below augments React's types instead of replacing them.
import 'react'

declare module 'react' {
  interface InputHTMLAttributes<T> extends HTMLAttributes<T> {
    webkitdirectory?: string
    directory?: string
  }
}

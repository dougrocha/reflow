import * as React from "react"
import { INPUT_CLASS } from "./input-class"
import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        INPUT_CLASS,
        "w-full",
        className
      )}
      {...props}
    />
  )
}

export { Input }

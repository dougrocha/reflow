import * as React from "react"
import { INPUT_CLASS } from "./input-class"
import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(INPUT_CLASS, "h-auto min-h-16 w-full py-1.5", className)}
      {...props}
    />
  )
}

export { Textarea }

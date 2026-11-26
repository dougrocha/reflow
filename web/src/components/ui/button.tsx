import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { type VariantProps } from "@/lib/variants"
import { cn } from "@/lib/utils"
import { buttonVariants } from "./button-variants"

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      // No caller passes Base UI's state function as className; one would be ignored here.
      className={cn(buttonVariants({ variant, size, className: typeof className === "string" ? className : undefined }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }

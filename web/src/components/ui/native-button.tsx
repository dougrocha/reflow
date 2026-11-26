import type { ComponentProps } from "react";
import type { VariantProps } from "@/lib/variants";
import { buttonVariants } from "./button-variants";

export function Button({ className, variant, size, type = "button", ...props }:
  ComponentProps<"button"> & VariantProps<typeof buttonVariants>) {
  return <button type={type} data-slot="button" className={buttonVariants({ variant, size, className })} {...props} />;
}

export { buttonVariants } from "./button-variants";

// A component's classes from its variant props: the base classes, each variant's classes for the
// value asked for (its default when the prop is undefined, none when it is null), then className.
// The classes are joined, not merged; a caller whose classes can conflict passes them to cn.

type Variants = Record<string, Record<string, string>>;
type Choice<V extends Variants> = { [K in keyof V]?: keyof V[K] | null };

export function variants<V extends Variants>(
  base: string,
  config: { variants: V; defaultVariants?: NoInfer<{ [K in keyof V]?: keyof V[K] }> },
) {
  return (props?: Choice<V> & { className?: string }): string => {
    const out = [base];
    for (const key in config.variants) {
      const asked = props?.[key];
      const value = asked === undefined ? config.defaultVariants?.[key] : asked;
      if (value != null) out.push(config.variants[key][value as string]);
    }
    out.push(props?.className ?? "");
    return out.filter(Boolean).join(" ");
  };
}

/** The variant props of a variants() function, for a component's props type. */
export type VariantProps<F> =
  F extends (props?: infer P) => string ? Omit<NonNullable<P>, "className"> : never;

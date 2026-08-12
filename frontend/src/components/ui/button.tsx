import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * The CVA button from the shadcn/originui set, with two deliberate departures.
 *
 * **Sizes are larger.** PRD §6 asks for targets a gloved hand can hit on a
 * tablet, so the default height is the `touch` token (3.5rem) rather than 2.25rem
 * and the type does not drop below the app's base size. A 32px-tall button is
 * fine on a desktop dashboard and wrong at a gate.
 *
 * **Focus rings are heavier.** Some of these screens are driven with a bluetooth
 * ring scanner and keyboard tabbing, so the focused control has to be obvious at
 * a glance rather than on inspection.
 */
const buttonVariants = cva(
  cn(
    'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl font-bold',
    'transition-all duration-200 active:scale-[0.98]',
    'outline-offset-2 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-blue-500/60',
    'disabled:pointer-events-none disabled:opacity-40',
    '[&_svg]:pointer-events-none [&_svg]:shrink-0',
  ),
  {
    variants: {
      variant: {
        default:
          'bg-gradient-to-b from-blue-600 to-blue-700 text-white shadow-lg shadow-blue-600/25 hover:from-blue-500 hover:to-blue-600',
        destructive:
          'bg-gradient-to-b from-bad to-red-800 text-white shadow-lg shadow-bad/25 hover:brightness-110',
        success:
          'bg-gradient-to-b from-ok to-green-800 text-white shadow-lg shadow-ok/25 hover:brightness-110',
        outline:
          'border-2 border-slate-300 bg-white/60 backdrop-blur hover:bg-white dark:border-white/15 dark:bg-white/5 dark:hover:bg-white/10',
        secondary:
          'bg-slate-100 text-slate-900 hover:bg-slate-200 dark:bg-white/10 dark:text-slate-100 dark:hover:bg-white/15',
        ghost: 'hover:bg-slate-100 dark:hover:bg-white/10',
        link: 'text-blue-700 underline-offset-4 hover:underline dark:text-blue-400',
      },
      size: {
        default: 'min-h-touch px-6 py-3 text-lg',
        sm: 'min-h-[2.75rem] rounded-lg px-4 text-base',
        lg: 'min-h-touch px-8 py-3 text-lg',
        icon: 'h-touch w-touch',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    )
  },
)
Button.displayName = 'Button'

export { Button, buttonVariants }

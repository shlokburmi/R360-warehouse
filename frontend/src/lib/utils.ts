import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Merge Tailwind classes with later ones winning.
 *
 * Plain string concatenation does not work for this: `"p-2" + " p-4"` leaves
 * both in the class list and the winner is whichever CSS rule Tailwind emitted
 * last, not the one the caller passed. `twMerge` resolves the conflict by
 * intent, which is what makes a `className` prop on a styled component
 * behave the way everyone expects it to.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

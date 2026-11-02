package com.lukatdamoves.karaokegen

import android.content.Context
import android.util.AttributeSet
import android.view.View
import android.view.ViewGroup

/**
 * Minimal wrapping layout: children flow left-to-right and wrap
 * to the next row when they no longer fit. Used for the editor's
 * word chips — one line of lyrics is a row of chips, and long lines
 * wrap instead of overflowing. No external dependency.
 */
class FlowLayout @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : ViewGroup(context, attrs) {

    private val spacingPx: Int
        get() = (6 * resources.displayMetrics.density).toInt()

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = MeasureSpec.getSize(widthMeasureSpec)
        val maxWidth = width - paddingLeft - paddingRight
        var rowWidth = 0
        var totalHeight = paddingTop + paddingBottom
        var rowHeight = 0
        for (i in 0 until childCount) {
            val child = getChildAt(i)
            if (child.visibility == View.GONE) continue
            measureChild(child, widthMeasureSpec, heightMeasureSpec)
            val cw = child.measuredWidth
            val ch = child.measuredHeight
            if (rowWidth > 0 && rowWidth + spacingPx + cw > maxWidth) {
                // Wrap to a new row.
                totalHeight += rowHeight + spacingPx
                rowWidth = cw
                rowHeight = ch
            } else {
                if (rowWidth > 0) rowWidth += spacingPx
                rowWidth += cw
                rowHeight = maxOf(rowHeight, ch)
            }
        }
        totalHeight += rowHeight
        setMeasuredDimension(
            width,
            resolveSize(totalHeight, heightMeasureSpec)
        )
    }

    override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {
        val maxWidth = r - l - paddingLeft - paddingRight
        var x = paddingLeft
        var y = paddingTop
        var rowHeight = 0
        for (i in 0 until childCount) {
            val child = getChildAt(i)
            if (child.visibility == View.GONE) continue
            val cw = child.measuredWidth
            val ch = child.measuredHeight
            if (x > paddingLeft && x + cw > paddingLeft + maxWidth) {
                // Wrap to a new row.
                x = paddingLeft
                y += rowHeight + spacingPx
                rowHeight = 0
            }
            child.layout(x, y, x + cw, y + ch)
            x += cw + spacingPx
            rowHeight = maxOf(rowHeight, ch)
        }
    }
}

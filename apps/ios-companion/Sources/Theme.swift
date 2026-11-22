import SwiftUI
import UIKit

/// Cohesive design system for JOAT Brain on iPhone & iPhone Duo, inspired by
/// Even Realities G2 phosphor emerald waveguide optics and warm stationery.
enum JoatTheme {
    /// G2 Phosphor Emerald: `#059669` in light mode, `#34D399` in dark mode.
    static let accent = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 52.0 / 255.0, green: 211.0 / 255.0, blue: 153.0 / 255.0, alpha: 1.0)
            : UIColor(red: 5.0 / 255.0, green: 150.0 / 255.0, blue: 105.0 / 255.0, alpha: 1.0)
    })

    /// Primary background canvas: warm stone-alabaster in light mode, deep obsidian in dark mode.
    static let surface = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 10.0 / 255.0, green: 14.0 / 255.0, blue: 13.0 / 255.0, alpha: 1.0)
            : UIColor(red: 245.0 / 255.0, green: 247.0 / 255.0, blue: 245.0 / 255.0, alpha: 1.0)
    })

    /// Elevated card surface over `surface`.
    static let card = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 20.0 / 255.0, green: 26.0 / 255.0, blue: 24.0 / 255.0, alpha: 1.0)
            : UIColor(red: 1.0, green: 1.0, blue: 1.0, alpha: 1.0)
    })

    /// Subtle hairline border for elevated cards.
    static let cardBorder = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 52.0 / 255.0, green: 211.0 / 255.0, blue: 153.0 / 255.0, alpha: 0.16)
            : UIColor(red: 15.0 / 255.0, green: 23.0 / 255.0, blue: 42.0 / 255.0, alpha: 0.08)
    })

    /// Hero card border with waveguide emerald tint.
    static let heroBorder = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 52.0 / 255.0, green: 211.0 / 255.0, blue: 153.0 / 255.0, alpha: 0.30)
            : UIColor(red: 5.0 / 255.0, green: 150.0 / 255.0, blue: 105.0 / 255.0, alpha: 0.22)
    })

    /// Warm ivory stationery (`#FAF8F4`) matching `Notebook.swift` sheets.
    static let paper = Color(red: 250.0 / 255.0, green: 248.0 / 255.0, blue: 244.0 / 255.0)

    /// Subtle stationery edge hairline.
    static let paperBorder = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 250.0 / 255.0, green: 248.0 / 255.0, blue: 244.0 / 255.0, alpha: 0.22)
            : UIColor(red: 180.0 / 255.0, green: 172.0 / 255.0, blue: 158.0 / 255.0, alpha: 0.38)
    })

    /// Adaptive amber status color for standby / action-needed states.
    static let amber = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 251.0 / 255.0, green: 191.0 / 255.0, blue: 36.0 / 255.0, alpha: 1.0)
            : UIColor(red: 217.0 / 255.0, green: 119.0 / 255.0, blue: 6.0 / 255.0, alpha: 1.0)
    })

    private static let heroStart = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 13.0 / 255.0, green: 44.0 / 255.0, blue: 34.0 / 255.0, alpha: 1.0)
            : UIColor(red: 230.0 / 255.0, green: 246.0 / 255.0, blue: 238.0 / 255.0, alpha: 1.0)
    })

    private static let heroMid = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 17.0 / 255.0, green: 28.0 / 255.0, blue: 25.0 / 255.0, alpha: 1.0)
            : UIColor(red: 243.0 / 255.0, green: 248.0 / 255.0, blue: 245.0 / 255.0, alpha: 1.0)
    })

    private static let heroEnd = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 15.0 / 255.0, green: 20.0 / 255.0, blue: 18.0 / 255.0, alpha: 1.0)
            : UIColor(red: 250.0 / 255.0, green: 250.0 / 255.0, blue: 248.0 / 255.0, alpha: 1.0)
    })

    /// Subtle dark-emerald-to-obsidian / warm-stone-to-sage hero card gradient.
    static var heroGradient: LinearGradient {
        LinearGradient(
            colors: [heroStart, heroMid, heroEnd],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }
}

extension Color {
    static var joatAccent: Color { JoatTheme.accent }
    static var joatSurface: Color { JoatTheme.surface }
    static var joatCard: Color { JoatTheme.card }
    static var joatPaper: Color { JoatTheme.paper }
}

/// Compact optical-style status pill used across Home, Mail, Notes, and Agents.
struct JoatStatusPill: View {
    let label: String
    var color: Color = JoatTheme.accent
    var icon: String? = nil

    var body: some View {
        HStack(spacing: 5) {
            if let icon {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .bold))
            } else {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
            }
            Text(label)
                .font(.caption2.weight(.semibold))
                .lineLimit(1)
        }
        .foregroundStyle(color)
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(color.opacity(0.14), in: Capsule())
        .overlay(
            Capsule()
                .strokeBorder(color.opacity(0.28), lineWidth: 0.75)
        )
    }
}

/// Top waveguide-inspired hero status card with optional action button and pill footer.
struct JoatHeroCard<Footer: View>: View {
    var eyebrow: String?
    let title: String
    let subtitle: String
    let icon: String
    var statusColor: Color = JoatTheme.accent
    var actionTitle: String? = nil
    var action: (() -> Void)? = nil
    @ViewBuilder let footer: () -> Footer

    init(
        eyebrow: String? = nil,
        title: String,
        subtitle: String,
        icon: String,
        statusColor: Color = JoatTheme.accent,
        actionTitle: String? = nil,
        action: (() -> Void)? = nil,
        @ViewBuilder footer: @escaping () -> Footer
    ) {
        self.eyebrow = eyebrow
        self.title = title
        self.subtitle = subtitle
        self.icon = icon
        self.statusColor = statusColor
        self.actionTitle = actionTitle
        self.action = action
        self.footer = footer
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 14) {
                ZStack {
                    RoundedRectangle(cornerRadius: 13, style: .continuous)
                        .fill(statusColor.opacity(0.16))
                        .frame(width: 44, height: 44)
                    RoundedRectangle(cornerRadius: 13, style: .continuous)
                        .strokeBorder(statusColor.opacity(0.32), lineWidth: 0.8)
                        .frame(width: 44, height: 44)
                    Image(systemName: icon)
                        .font(.system(size: 19, weight: .semibold))
                        .foregroundStyle(statusColor)
                }

                VStack(alignment: .leading, spacing: 4) {
                    if let eyebrow, !eyebrow.isEmpty {
                        Text(eyebrow.uppercased())
                            .font(.system(size: 10, weight: .bold, design: .rounded))
                            .tracking(0.8)
                            .foregroundStyle(statusColor)
                    }
                    Text(title)
                        .font(.headline.weight(.bold))
                        .foregroundStyle(.primary)
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer(minLength: 4)

                if let actionTitle, let action {
                    Button(actionTitle, action: action)
                        .font(.footnote.weight(.semibold))
                        .buttonStyle(.borderedProminent)
                        .tint(JoatTheme.accent)
                        .controlSize(.small)
                }
            }

            footer()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(JoatTheme.heroGradient)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(JoatTheme.heroBorder, lineWidth: 1)
        )
    }
}

extension JoatHeroCard where Footer == EmptyView {
    init(
        eyebrow: String? = nil,
        title: String,
        subtitle: String,
        icon: String,
        statusColor: Color = JoatTheme.accent,
        actionTitle: String? = nil,
        action: (() -> Void)? = nil
    ) {
        self.init(
            eyebrow: eyebrow,
            title: title,
            subtitle: subtitle,
            icon: icon,
            statusColor: statusColor,
            actionTitle: actionTitle,
            action: action
        ) {
            EmptyView()
        }
    }
}

/// Subsystem status tile for 2×2 grids on Home.
struct JoatMetricTile: View {
    let title: String
    let value: String
    var detail: String? = nil
    let icon: String
    var tint: Color = JoatTheme.accent
    var action: (() -> Void)? = nil

    var body: some View {
        Group {
            if let action {
                Button(action: action) {
                    tileContent(interactive: true)
                }
                .buttonStyle(.plain)
            } else {
                tileContent(interactive: false)
            }
        }
    }

    private func tileContent(interactive: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                ZStack {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .fill(tint.opacity(0.14))
                        .frame(width: 32, height: 32)
                    Image(systemName: icon)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(tint)
                }
                Spacer()
                if interactive {
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.tertiary)
                } else {
                    Circle()
                        .fill(tint)
                        .frame(width: 7, height: 7)
                }
            }

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(value)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                if let detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(JoatTheme.card)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
        )
    }
}

extension View {
    /// Applies elevated JOAT card surface and hairline border.
    func joatCardContainer(cornerRadius: CGFloat = 16, padding: CGFloat = 14) -> some View {
        self
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .fill(JoatTheme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(JoatTheme.cardBorder, lineWidth: 1)
            )
    }
}

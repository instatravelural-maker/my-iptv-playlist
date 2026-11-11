
            if not feed:
                stats["Нет подтверждения языка"] += 1
                continue

            if not language_is_russian(feed.get("languages")):
                stats["Другой язык"] += 1
                continue

            group = channel_group(channel, stream)
            stats["Русскоязычные потоки"] += 1

        if stream_url in seen_urls:
            stats["Дубликаты ссылок"] += 1
            continue

        seen_urls.add(stream_url)

        quality = clean(stream.get("quality"))

        entries.append({
            "channel": channel,
            "stream": stream,
            "group": group,
            "url": stream_url,
            "quality": quality,
        })

    # Сортируем по категории, названию и качеству.
    entries.sort(
        key=lambda item: (
            item["group"].lower(),
            item["channel"].get("name", "").lower(),
            item["quality"],
        )
    )

    for item in entries:
        output.append(
            make_extinf(
                item["channel"],
                item["stream"],
                item["group"],
            )
        )

        add_headers(output, item["stream"])
        output.append(item["url"])

        counts[item["group"]] += 1

    # Дополнительные списки, добавленные вручную.
    # Считаем их проверенными пользователем.
    add_manual_file(
        INTERNATIONAL_FILE,
        "Зарубежные на русском",
        output,
        seen_urls,
        counts,
        stats,
    )

    add_manual_file(
        REGIONAL_FILE,
        "Региональные",
        output,
        seen_urls,
        counts,
        stats,
    )

    if len(entries) < 100:
        raise RuntimeError(
            "Получено подозрительно мало каналов. "
            "Старый playlist.m3u оставлен без изменений."
        )

    invalid = [
        line for line in output
        if line.startswith("#EXTINF:")
        and not re.match(
            r"^#EXTINF:-?\d+(?:\.\d+)?(?:\s|,)",
            line,
        )
    ]

    if invalid:
        raise ValueError(
            f"Ошибка формата M3U: {invalid[0]}"
        )

    OUT.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )

    print("\n========== РЕЗУЛЬТАТ ==========")
    print("Всего потоков:", len(entries))
    print("Всего каналов в плейлисте:", sum(counts.values()))

    print("\nФильтрация:")
    for name, count in sorted(stats.items()):
        print(f"  {name}: {count}")

    print("\nКатегории:")
    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")

    print("\nПлейлист успешно создан:", OUT)


if __name__ == "__main__":
    main()

"""Original text conversion retained only as a regression fixture."""
layout_01 = '`~@#$%^&' + 'qwertyuiop[]QWERTYUIOP{}asdfghjkl;\'\\ASDFGHJKL:"|zxcvbnm,./ZXCVBNM<>?'
layout_02 = 'ёЁ"№;%:?' + 'йцукенгшщзхъЙЦУКЕНГШЩЗХЪфывапролджэ\\ФЫВАПРОЛДЖЭ/ячсмитьбю.ЯЧСМИТЬБЮ,'

def switch_layout(text):
    """Переключение раскладки текста"""
    if len(text) < 1:
        return text
    first_x = min(3, len(text))
    (chars_01, chars_02) = (0, 0)
    for i in range(first_x):
        if text[i] in layout_01:
            chars_01 += 1
        elif text[i] in layout_02:
            chars_02 += 1
    if chars_01 > chars_02:
        layout = layout_01 + layout_02 + layout_01
    else:
        layout = layout_02 + layout_01 + layout_02
    new_text = ''
    for char in text:
        if char in layout:
            new_pos = layout.find(char) + len(layout_01)
            if new_pos < len(layout):
                new_text += layout[new_pos]
            else:
                new_text += char
        else:
            new_text += char
    return new_text

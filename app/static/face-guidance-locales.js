(function () {
  const messages = {
    ru: {
      moveLeft: 'Сместитесь немного влево', moveRight: 'Сместитесь немного вправо', moveUp: 'Поднимите голову', moveDown: 'Опустите голову', neutral: 'Поместите лицо в контур', starting: 'Запускаем камеру…', hold: 'Смотрите прямо и не двигайтесь',
      ready: 'Положение правильное', done: 'Готово', processing: 'Проверяем лицо…', verified: 'Личность подтверждена',
      unknown: 'Лицо не распознано', unknownDetail: 'Пользователь с таким лицом не найден',
      ambiguous: 'Не удалось распознать лицо', blocked: 'Доступ заблокирован', disabled: 'Учетная запись отключена',
      denied: 'Доступ отклонен', retry: 'Попробовать снова', retryDetail: 'Попробуйте еще раз',
      failed: 'Не удалось выполнить проверку лица', cameraDenied: 'Разрешите доступ к камере', cameraUnavailable: 'Камера недоступна или занята',
      noFace: 'Поместите лицо в контур', small: 'Подойдите ближе', multiple: 'В кадре должно быть одно лицо',
      blurry: 'Не двигайтесь: изображение размыто', lighting: 'Улучшите освещение лица', wrong: 'Повернитесь в указанную сторону',
      timeout: 'Время проверки истекло', limited: 'Слишком много попыток. Повторите позже',
      capturing: 'Смотрите прямо. Получаем фотографии', submitted: 'Заявка отправлена на проверку',
      CENTER: 'Смотрите прямо в камеру', TURN_LEFT: 'Поверните голову влево', TURN_RIGHT: 'Поверните голову вправо', BLINK: 'Моргните',
      turnMoreLeft: 'Поверните голову немного сильнее влево', turnMoreRight: 'Поверните голову немного сильнее вправо', blinkCenter: 'Посмотрите прямо и моргните',
    },
    en: {
      moveLeft: 'Move slightly left', moveRight: 'Move slightly right', moveUp: 'Move your head up', moveDown: 'Move your head down', neutral: 'Position your face in the guide', starting: 'Starting camera…', hold: 'Look straight ahead and hold still',
      ready: 'Position is correct', done: 'Done', processing: 'Verifying your face…', verified: 'Identity verified',
      unknown: 'Face not recognized', unknownDetail: 'No matching user was found',
      ambiguous: 'Unable to recognize your face', blocked: 'Access blocked', disabled: 'Account is disabled',
      denied: 'Access denied', retry: 'Try again', retryDetail: 'Please try again',
      failed: 'Unable to verify face', cameraDenied: 'Allow camera access', cameraUnavailable: 'Camera is unavailable or busy',
      noFace: 'Position your face in the guide', small: 'Move closer', multiple: 'Only one face should be in the frame',
      blurry: 'Hold still: the image is blurry', lighting: 'Improve lighting on your face', wrong: 'Turn in the indicated direction',
      timeout: 'Verification timed out', limited: 'Too many attempts. Try again later',
      capturing: 'Look straight ahead. Capturing photos', submitted: 'Request submitted for review',
      CENTER: 'Look straight into the camera', TURN_LEFT: 'Turn your head left', TURN_RIGHT: 'Turn your head right', BLINK: 'Blink',
      turnMoreLeft: 'Turn your head slightly farther left', turnMoreRight: 'Turn your head slightly farther right', blinkCenter: 'Look straight ahead and blink',
    },
  };
  for (const [lang, entries] of Object.entries(messages)) {
    const prefixed = Object.fromEntries(Object.entries(entries).map(([key, value]) => ['fg_' + key, value]));
    if (typeof BioGateV3Messages !== 'undefined') Object.assign(BioGateV3Messages[lang], prefixed);
    if (window.BioGateI18n) Object.assign(window.BioGateI18n[lang], prefixed);
  }
}());

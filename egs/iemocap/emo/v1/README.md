This recipe do emotion recognition on IEMOCAP.

This recipe do emotion recognition on `improvised` and `scripted` speech.

| model | config | data |  acc | baseline | reference|
| ---   | ---    | ---  |  --- | ---      |  ---     |
| resnet50 | conf/emo-keras-resnet50.ymal | all | 59.15 | 56.10 | Neumann and Vu (2017) |
| rnn-mean pool | conf/emo-keras-blstm.yaml | impro |  65.23 | 56.90 | Mirsamadi et al. (2017) |

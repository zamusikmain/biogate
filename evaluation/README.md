# Local evaluation dataset

Create a private, consented dataset outside Git. Add a CSV manifest:

```csv
image_a,image_b,is_genuine
person1/a.jpg,person1/b.jpg,true
person1/a.jpg,person2/a.jpg,false
```

Paths are relative to the manifest. Include representative cameras, lighting, poses, demographic groups, genuine pairs, and impostor pairs. Never infer FAR/FRR from only genuine pairs. Run `python -m evaluation.run evaluation/datasets/pairs.csv`. Output is ignored by Git.


import os

os.system("wget https://www.mit.edu/~mlechner/walker.zip")
print("Download complete for Walker dataset")
os.system("unzip walker.zip -d data/")
print("Install complete for Walker dataset")

os.system("mkdir output")
